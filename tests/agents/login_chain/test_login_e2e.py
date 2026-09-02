"""Login is page 1 of the same chain: the chain drives it, offline.

Real browser, real Scraper (with its model replaced by the payload echo), real
Frontier, real Loop, against the stand-in portal: sign in, clear the emailed
code, land on the dashboard. In the FormFiller's slot sits `tests/login_executor`,
a test-only executor built on the toolkit's login actions, because the FormFiller
agent is another team's work and does not exist yet. When it lands, it takes
this seat and this test should pass unchanged.
"""

from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.scraper import Scraper
from trailblazer.contracts import LOGIN_EMAIL, LOGIN_OTP, LOGIN_PASSWORD, Walk
from trailblazer.loop.orchestrator import Loop
from trailblazer.shared.carrier_creds import CarrierCreds, MfaConfig
from trailblazer.shared.config import Settings
from tests.echo_model import install_echo_model
from tests.login_executor import LoginExecutor

SETTINGS = Settings(_env_file=None)


def _creds(base: str, password: str = "pw-1", page: str = "login.html") -> CarrierCreds:
    return CarrierCreds(
        slug="pie",
        login_url=f"{base}/{page}",
        username="agent@example.com",
        password=password,
        mfa=MfaConfig(enabled=True),
    )


def _run(port: int, creds: CarrierCreds, inbox_url: str) -> tuple[Walk, FrontierAgent, str]:
    with BrowserSession(cdp_port=port) as session:
        page = session.goto(creds.login_url)
        scraper = Scraper(page, SETTINGS)
        frontier = FrontierAgent()
        executor = LoginExecutor(
            page,
            credentials=creds,
            inbox=OtpInbox(inbox_url, "a", "c", backoff_s=0, retries=1),
            human_entry_possible=False,
            mfa_timeout_s=20,
            poll_s=0.2,
            settle_s=0.5,
        )
        first, _ = scraper.look("job")
        walk = Loop(scraper, frontier, executor).fill_form("job", first)
        return walk, frontier, page.url


def brief(steps):
    return [(s.action, s.locator, s.credentialKey or s.option or s.value) for s in steps]


def test_the_chain_signs_in_clears_the_code_and_publishes_a_login_prefix(
    fixture_server, fake_inbox, monkeypatch
) -> None:
    install_echo_model(monkeypatch)
    inbox = fake_inbox([(200, {"code": "123456"})])

    walk, frontier, final_url = _run(9281, _creds(fixture_server), inbox.url)

    assert final_url.endswith("/dashboard.html")
    assert brief(walk.login) == [
        ("type", "#username", LOGIN_EMAIL),
        ("type", "#password", LOGIN_PASSWORD),
        ("click", 'button:has-text("Sign in") >> visible=true', None),
        ("type", "#code", LOGIN_OTP),
    ]
    assert walk.paths == []  # the dashboard has no form; nothing to walk
    assert frontier.board.status == "complete"
    # The stages the Scraper named, from the measured credentials.
    assert [c.stageId for c in frontier.board.controls][:3] == ["login_login", "login_login", "login_login"]
    assert any(c.stageId == "login_otp" for c in frontier.board.controls)
    # The secret never entered any contract object.
    assert "pw-1" not in walk.model_dump_json()
    assert "123456" not in walk.model_dump_json()


def test_a_wrong_password_stops_the_chain_with_auth(fixture_server, fake_inbox, monkeypatch) -> None:
    install_echo_model(monkeypatch)
    inbox = fake_inbox([])

    walk, frontier, final_url = _run(9282, _creds(fixture_server, password="wrong"), inbox.url)

    assert final_url.endswith("/login.html")
    assert walk == Walk()  # nothing published
    assert frontier.board.status == "blocked"


def test_a_consent_banner_is_dismissed_and_the_dismissal_is_in_the_login_prefix(
    fixture_server, fake_inbox, monkeypatch
) -> None:
    install_echo_model(monkeypatch)
    inbox = fake_inbox([(200, {"code": "123456"})])

    walk, _, final_url = _run(9283, _creds(fixture_server, page="login-consent.html"), inbox.url)

    assert final_url.endswith("/dashboard.html")
    first = walk.login[0]
    assert first.action == "click" and "reject" in first.locator.lower()
    assert brief(walk.login)[1:] == [
        ("type", "#username", LOGIN_EMAIL),
        ("type", "#password", LOGIN_PASSWORD),
        ("click", 'button:has-text("Sign in") >> visible=true', None),
        ("type", "#code", LOGIN_OTP),
    ]


def test_the_captured_prefix_replays_in_a_fresh_browser(fixture_server, fake_inbox, monkeypatch) -> None:
    """A recipe is trustworthy only after it has driven a login itself: capture, then replay."""
    from trailblazer.agents.browser.login_replay import replay_login

    install_echo_model(monkeypatch)
    capture_inbox = fake_inbox([(200, {"code": "123456"})])
    walk, _, _ = _run(9284, _creds(fixture_server), capture_inbox.url)
    assert walk.login

    replay_inbox = fake_inbox([(200, {"code": "123456"})])
    with BrowserSession(cdp_port=9285) as fresh:
        out = replay_login(
            fresh.page,
            walk.login,
            credentials=_creds(fixture_server),
            inbox=OtpInbox(replay_inbox.url, "a", "c", backoff_s=0, retries=1),
            login_url=_creds(fixture_server).login_url,
            human_entry_possible=False,
            mfa_timeout_s=20,
            poll_s=0.2,
            otp_settle_s=0.5,
            step_timeout_ms=3000,
            verify_settle_s=2,
        )
    assert out.ok, out.reason
    assert out.final_url.endswith("/dashboard.html")


def test_ensure_login_captures_on_the_first_run_and_replays_on_the_second(
    fixture_server, fake_inbox, monkeypatch, tmp_path
) -> None:
    """Loop's whole login path against the stand-in portal: learn once, then reuse.

    The stand-in portal keeps no server-side session, so the saved jar cannot
    hold and the second run must fall back to the stored prefix, which is the
    route under test. The lock is the real one when Postgres is up, and fails
    open when it is not.
    """
    from trailblazer.agents.browser.session_store import SessionStore
    from trailblazer.loop.login import carrier_tab, ensure_login
    from tests.fakes import FakeProgramStore

    install_echo_model(monkeypatch)
    creds = _creds(fixture_server)
    store = SessionStore(tmp_path)
    programs = FakeProgramStore()

    # Run 1: nothing stored, an executor available -> capture and publish v1.
    inbox1 = fake_inbox([(200, {"code": "123456"})])
    settings1 = Settings(_env_file=None, cdp_port=9286, mfa_poll_s=0.2, mfa_timeout_ms=20_000)
    with carrier_tab(creds, settings1, store=store) as session:
        client = OtpInbox(inbox1.url, "a", "c", backoff_s=0, retries=1)
        outcome1, page1, _, _ = ensure_login(
            "run-1",
            session,
            creds,
            scraper=Scraper(session.page, settings1),
            frontier=FrontierAgent(),
            programs=programs,
            executor=LoginExecutor(
                session.page, credentials=creds, inbox=client, mfa_timeout_s=20, poll_s=0.2, settle_s=0.5
            ),
            inbox=client,
            settings=settings1,
        )
    assert outcome1.status == "captured", outcome1.reason
    assert outcome1.program_version == 1 and len(outcome1.steps) == 4
    assert page1.stageId.startswith("form_page")
    assert store.jar_path("pie").exists()  # the session was saved on the way out

    # Run 2: no executor at all -> the stored prefix must carry the login.
    inbox2 = fake_inbox([(200, {"code": "123456"})])
    settings2 = Settings(_env_file=None, cdp_port=9287, mfa_poll_s=0.2, mfa_timeout_ms=20_000)
    with carrier_tab(creds, settings2, store=store) as session:
        assert session.reused_session  # the jar from run 1 was restored
        outcome2, page2, _, _ = ensure_login(
            "run-2",
            session,
            creds,
            scraper=Scraper(session.page, settings2),
            frontier=FrontierAgent(),
            programs=programs,
            inbox=OtpInbox(inbox2.url, "a", "c", backoff_s=0, retries=1),
            settings=settings2,
        )
    assert outcome2.status == "replayed", outcome2.reason
    assert outcome2.program_version == 1
    assert programs.rows[0].status == "locked"  # proven by a replay, promoted
    assert page2.stageId.startswith("form_page")
