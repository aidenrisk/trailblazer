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
