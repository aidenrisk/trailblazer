"""Replaying a captured login prefix in a fresh browser, against the stand-in portal.

The prefix below is exactly what the chain publishes for login.html (see
tests/agents/login_chain/test_login_e2e.py), written out so these tests need no
Scraper. The last test captures one for real and replays it.
"""

from trailblazer.agents.browser.login_replay import replay_login, verify_logged_in
from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.agents.browser.session import BrowserSession
from trailblazer.contracts import LOGIN_EMAIL, LOGIN_OTP, LOGIN_PASSWORD, WalkStep
from trailblazer.shared.carrier_creds import CarrierCreds, MfaConfig

CREDS = CarrierCreds(
    slug="pie", login_url="https://x", username="agent@example.com", password="pw-1", mfa=MfaConfig(enabled=True)
)

PREFIX = [
    WalkStep(action="type", fieldId="q_001", locator="#username", credentialKey=LOGIN_EMAIL),
    WalkStep(action="type", fieldId="q_002", locator="#password", credentialKey=LOGIN_PASSWORD),
    WalkStep(action="click", locator='button:has-text("Sign in") >> visible=true'),
    WalkStep(action="type", fieldId="q_001", locator="#code", credentialKey=LOGIN_OTP),
]


def _replay(port, base, steps, inbox_url, creds=CREDS, **kw):
    with BrowserSession(cdp_port=port) as s:
        out = replay_login(
            s.page,
            steps,
            credentials=creds,
            inbox=OtpInbox(inbox_url, "a", "c", backoff_s=0, retries=1),
            login_url=f"{base}/{kw.pop('page', 'login.html')}",
            human_entry_possible=False,
            mfa_timeout_s=kw.pop("mfa_timeout_s", 20),
            poll_s=0.2,
            otp_settle_s=0.5,
            step_timeout_ms=kw.pop("step_timeout_ms", 3000),
            verify_settle_s=kw.pop("verify_settle_s", 2),
            **kw,
        )
    return out


def test_a_good_prefix_replays_to_the_dashboard(fixture_server, fake_inbox) -> None:
    inbox = fake_inbox([(200, {"code": "123456"})])
    out = _replay(9291, fixture_server, PREFIX, inbox.url)
    assert out.ok, out.reason
    assert out.final_url.endswith("/dashboard.html")
    assert not out.degrades_artifact


def test_a_drifted_selector_is_a_defect_naming_the_step(fixture_server, fake_inbox) -> None:
    inbox = fake_inbox([])
    drifted = [PREFIX[0], PREFIX[1].model_copy(update={"locator": "#passwd"}), *PREFIX[2:]]
    out = _replay(9292, fixture_server, drifted, inbox.url)
    assert out.kind == "defect" and out.degrades_artifact
    assert out.step_index == 1 and out.step.locator == "#passwd"
    assert "not_found" in out.reason


def test_wrong_credentials_are_auth_not_drift(fixture_server, fake_inbox) -> None:
    """The recipe is fine; the portal said no. The stored prefix must survive this."""
    inbox = fake_inbox([])
    bad = CREDS.model_copy(update={"password": "wrong"})
    out = _replay(9293, fixture_server, PREFIX, inbox.url, creds=bad)
    assert out.kind == "auth" and not out.degrades_artifact
    assert out.final_url.endswith("/login.html")
    assert out.step_index == 3  # the code input never appeared, because sign-in did not move on


def test_a_code_that_never_arrives_is_mfa_timeout(fixture_server, fake_inbox) -> None:
    inbox = fake_inbox([])
    out = _replay(9294, fixture_server, PREFIX, inbox.url, mfa_timeout_s=1.5)
    assert out.kind == "mfa_timeout" and not out.degrades_artifact
    assert out.step_index == 3 and out.otp is not None and out.otp.fetches >= 1


def test_a_consent_banner_in_the_prefix_is_replayed_first(fixture_server, fake_inbox) -> None:
    inbox = fake_inbox([(200, {"code": "123456"})])
    reject = 'button:has-text("reject all")'
    out = _replay(9295, fixture_server, [WalkStep(action="click", locator=reject), *PREFIX], inbox.url, page="login-consent.html")
    assert out.ok, out.reason


def test_a_missing_credential_on_file_is_auth(fixture_server, fake_inbox) -> None:
    inbox = fake_inbox([])
    otp_only = CarrierCreds(slug="pie", login_url="https://x", username="agent@example.com", mfa=MfaConfig(enabled=True))
    out = _replay(9296, fixture_server, PREFIX, inbox.url, creds=otp_only)
    assert out.kind == "auth" and out.step_index == 1


def test_an_empty_prefix_is_a_defect_and_a_bad_url_is_browser(fixture_server, fake_inbox) -> None:
    inbox = fake_inbox([])
    with BrowserSession(cdp_port=9297) as s:
        empty = replay_login(s.page, [], credentials=CREDS)
        dead = replay_login(s.page, PREFIX, credentials=CREDS, login_url="http://127.0.0.1:9/nowhere", step_timeout_ms=500)
    assert empty.kind == "defect"
    assert dead.kind == "browser"


def test_verify_logged_in_uses_the_prefixes_own_inputs(fixture_server) -> None:
    with BrowserSession(cdp_port=9298) as s:
        s.goto(f"{fixture_server}/login.html")
        on_login, why = verify_logged_in(s.page, PREFIX, settle_s=0.5)
        s.goto(f"{fixture_server}/dashboard.html")
        on_dash, _ = verify_logged_in(s.page, PREFIX, settle_s=0.5)
    assert not on_login and "still on the login screen" in why
    assert on_dash
