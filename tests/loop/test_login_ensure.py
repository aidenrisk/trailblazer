"""Loop's ensure_login, every route, offline.

The Scraper and FormFiller seats are stubs, the replayer is a fake that returns
whatever kind the test wants, the program store is a dict, and the lock records
what was done to it. What is under test is the decision: which route was taken,
what was published or degraded, when the lock was held and released, when the
session was saved.
"""

import pytest

from trailblazer.agents.browser.login_replay import LoginReplay
from trailblazer.agents.form_filler.stub import StubFormFiller
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.stub import StubScraper
from trailblazer.contracts import LOGIN_EMAIL, LOGIN_OTP, LOGIN_PASSWORD, Diff, PageDescription, WalkStep
from trailblazer.loop.login import capture_login, ensure_login, login_test
from trailblazer.shared.carrier_creds import CarrierCreds, MfaConfig
from trailblazer.shared.config import Settings
from tests.agents.frontier.frontier_test_data import FORM_AFTER_LOGIN, LOGIN_OTP_PAGE, LOGIN_PAGE
from tests.fakes import FakeLock, FakeProgramStore

SETTINGS = Settings(_env_file=None, login_lock_acquire_timeout_s=1, login_lock_max_hold_s=5)
CREDS = CarrierCreds(slug="pie", login_url="https://portal.example/login", username="u", password="p", mfa=MfaConfig(enabled=True))
NO_MFA = CarrierCreds(slug="pie", login_url="https://portal.example/login", username="u", password="p")
PREFIX = [
    WalkStep(action="type", fieldId="q_user", locator="#username", credentialKey=LOGIN_EMAIL),
    WalkStep(action="type", fieldId="q_pass", locator="#password", credentialKey=LOGIN_PASSWORD),
    WalkStep(action="click", locator='button:has-text("Sign in")'),
]


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakePage:
    url = "https://portal.example/login"

    def goto(self, url, **kw):
        self.url = url

    def evaluate(self, script):
        return None


class FakeContext:
    def clear_cookies(self):
        pass


class FakeSession:
    page = FakePage()
    context = FakeContext()


class SeqScraper:
    """Returns the scripted pages, one per look, in order (the last one repeats)."""

    def __init__(self, *pages: dict):
        self.pages = [PageDescription(**p) for p in pages]
        self.looks = 0

    def look(self, job, objective="perceive", last_assignment=None, fill_report=None):
        page = self.pages[min(self.looks, len(self.pages) - 1)]
        self.looks += 1
        return page, Diff(polarity="-ve")


def fake_replay(kind: str, final="https://portal.example/dashboard"):
    def replay(page, steps, **kw):
        return LoginReplay(kind, f"faked {kind}", final_url=final, step_index=1 if kind == "defect" else None)

    return replay


@pytest.fixture(autouse=True)
def _clear_locks():
    FakeLock.made.clear()
    yield


def run(scraper, store, *, creds=CREDS, replay=None, executor=None, frontier=None, saved=None):
    saves = saved if saved is not None else []
    outcome, page, diff, report = ensure_login(
        "job",
        FakeSession(),
        creds,
        scraper=scraper,
        frontier=frontier or FrontierAgent(),
        programs=store,
        executor=executor,
        settings=SETTINGS,
        lock_url="postgresql://unused",
        replay=replay or fake_replay("ok"),
        lock_factory=FakeLock,
        save_session=lambda: saves.append("saved"),
    )
    return outcome, page, saves


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def test_a_session_that_held_skips_login_lock_and_replay() -> None:
    outcome, page, saves = run(SeqScraper(FORM_AFTER_LOGIN), FakeProgramStore("pie", PREFIX))

    assert outcome.status == "session_held" and outcome.ok
    assert page.stageId == "form_page_1_business_info"
    assert FakeLock.made == []  # never even built
    assert saves == []


def test_a_stored_prefix_that_replays_is_promoted_and_the_session_saved() -> None:
    store = FakeProgramStore("pie", PREFIX)
    outcome, page, saves = run(SeqScraper(LOGIN_PAGE, FORM_AFTER_LOGIN), store, replay=fake_replay("ok"))

    assert outcome.status == "replayed" and outcome.program_version == 1
    assert store.rows[0].status == "locked"  # a candidate that replayed is proven
    assert page.stageId == "form_page_1_business_info"
    assert saves == ["saved"]
    assert FakeLock.made[0].events == ["acquire", "release"]


def test_a_broken_prefix_is_degraded_and_the_login_is_learned_again() -> None:
    store = FakeProgramStore("pie", PREFIX)
    # Looks: login (first), login (after reset), then the stub scraper is not used for capture here;
    # capture runs through the StubScraper chain, so give the capture its own scraper via a combined stub.
    scraper = StubScraper([PageDescription(**LOGIN_PAGE), PageDescription(**LOGIN_OTP_PAGE), PageDescription(**FORM_AFTER_LOGIN)])
    outcome, page, saves = run(scraper, store, replay=fake_replay("defect"), executor=StubFormFiller())

    assert outcome.status == "captured"
    assert outcome.degraded_version == 1 and store.degraded[0][0] == 1
    assert outcome.program_version == 2 and store.active("pie").version == 2
    assert [s.credentialKey or s.action for s in outcome.steps] == [LOGIN_EMAIL, LOGIN_PASSWORD, "click", LOGIN_OTP, "click"]
    assert page.stageId == "form_page_1_business_info"
    assert saves == ["saved"]


def test_wrong_credentials_keep_the_prefix_and_do_not_capture() -> None:
    store = FakeProgramStore("pie", PREFIX)
    outcome, _, saves = run(SeqScraper(LOGIN_PAGE), store, replay=fake_replay("auth"), executor=StubFormFiller())

    assert outcome.status == "auth" and not outcome.ok
    assert store.degraded == [] and len(store.rows) == 1
    assert saves == []
    assert FakeLock.made[0].events == ["acquire", "release"]  # released even on failure


def test_no_prefix_and_no_executor_is_needs_authoring() -> None:
    outcome, _, _ = run(SeqScraper(LOGIN_PAGE), FakeProgramStore())
    assert outcome.status == "needs_authoring"
    assert "no FormFiller" in outcome.reason


def test_no_prefix_with_an_executor_captures_and_publishes_v1() -> None:
    store = FakeProgramStore()
    scraper = StubScraper([PageDescription(**LOGIN_PAGE), PageDescription(**LOGIN_OTP_PAGE), PageDescription(**FORM_AFTER_LOGIN)])
    outcome, page, saves = run(scraper, store, executor=StubFormFiller())

    assert outcome.status == "captured" and outcome.program_version == 1
    assert store.active("pie").steps == outcome.steps
    assert all(s.value is None for s in outcome.steps if s.credentialKey)
    assert page.stageId == "form_page_1_business_info"


def test_a_carrier_without_mfa_takes_no_lock() -> None:
    outcome, _, _ = run(SeqScraper(LOGIN_PAGE), FakeProgramStore("pie", PREFIX), creds=NO_MFA, replay=fake_replay("ok"))
    assert outcome.status == "replayed"
    assert FakeLock.made[0].slug is None and FakeLock.made[0].events == ["noop", "release"]


def test_a_rejected_capture_stops_with_auth() -> None:
    """Only the login page is scripted: Sign in leaves the stub where it was, Frontier reads rejection."""
    outcome, _, _ = run(StubScraper([PageDescription(**LOGIN_PAGE)]), FakeProgramStore(), executor=StubFormFiller())
    assert outcome.status == "auth"
    assert "frontier stopped" in outcome.reason


# --------------------------------------------------------------------------- #
# capture_login on its own, and login_test
# --------------------------------------------------------------------------- #


def test_capture_login_absorbs_the_last_action_and_hands_the_form_walk_its_diff() -> None:
    scraper = StubScraper([PageDescription(**LOGIN_PAGE), PageDescription(**FORM_AFTER_LOGIN)])
    frontier = FrontierAgent()
    page, diff = scraper.look("job")

    status, first_form_page, diff, report, _ = capture_login(
        "job", page, diff, scraper=scraper, frontier=frontier, executor=StubFormFiller()
    )

    assert status == "captured" and first_form_page.stageId == "form_page_1_business_info"
    assert report is None  # absorbed at the boundary, so the prefix is complete
    assert diff is not None  # the first form page's diff, for the walk's first call
    assert [s.credentialKey or s.action for s in frontier.login_prefix()] == [LOGIN_EMAIL, LOGIN_PASSWORD, "click"]


def test_login_test_reports_and_degrades_only_on_defect() -> None:
    store = FakeProgramStore("pie", PREFIX)
    ok = login_test(FakeSession(), CREDS, programs=store, settings=SETTINGS, replay=fake_replay("ok"))
    assert ok.status == "replayed" and ok.program_version == 1 and store.degraded == []

    bad = login_test(FakeSession(), CREDS, programs=store, settings=SETTINGS, replay=fake_replay("auth"))
    assert bad.status == "auth" and store.degraded == []

    broke = login_test(FakeSession(), CREDS, programs=store, settings=SETTINGS, replay=fake_replay("defect"))
    assert broke.status == "defect" and broke.degraded_version == 1 and store.active("pie") is None

    none = login_test(FakeSession(), CREDS, programs=store, settings=SETTINGS)
    assert none.status == "needs_authoring"
