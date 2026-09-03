"""The login actions a FormFiller calls, each against a stand-in page. Real browser, no model."""

from trailblazer.agents.browser import login_actions as la
from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.agents.browser.session import BrowserSession
from trailblazer.contracts import LOGIN_EMAIL, LOGIN_PASSWORD
from trailblazer.shared.carrier_creds import CarrierCreds, MfaConfig

CREDS = CarrierCreds(
    slug="pie", login_url="https://x", username="agent@example.com", password="pw-1", mfa=MfaConfig(enabled=True)
)


def test_fill_credential_types_the_secret_and_returns_only_the_selector(fixture_server) -> None:
    with BrowserSession(cdp_port=9261) as s:
        page = s.goto(f"{fixture_server}/login.html")
        out = la.fill_credential(page, "#username", LOGIN_EMAIL, CREDS)
        typed = page.input_value("#username")
    assert out.ok and out.selector == "#username" and out.error is None
    assert typed == "agent@example.com"
    assert "agent@example.com" not in repr(out)


def test_a_missing_credential_is_auth_not_a_widget_failure(fixture_server) -> None:
    otp_only = CarrierCreds(slug="next", login_url="https://x", username="u")
    with BrowserSession(cdp_port=9262) as s:
        page = s.goto(f"{fixture_server}/login.html")
        out = la.fill_credential(page, "#password", LOGIN_PASSWORD, otp_only)
    assert not out.ok and out.error == "auth"


def test_clear_otp_pulls_the_code_and_the_page_leaves_the_challenge(fixture_server, fake_inbox) -> None:
    server = fake_inbox([(200, {"code": "123456"})])
    with BrowserSession(cdp_port=9263) as s:
        page = s.goto(f"{fixture_server}/otp.html")
        out = la.clear_otp(
            page,
            "#code",
            CREDS,
            OtpInbox(server.url, "a", "c", backoff_s=0, retries=1),
            human_entry_possible=False,
            timeout_s=20,
            poll_s=0.2,
            settle_s=0.5,
            markers=["/otp"],
        )
        landed = page.url
    assert out.cleared and landed.endswith("/dashboard.html")
    assert server.calls and server.calls[0]["path"] == "/api/internal/mfa/pie/otp"  # keyed by the carrier slug


def test_a_code_that_never_arrives_maps_to_mfa_timeout(fixture_server, fake_inbox) -> None:
    server = fake_inbox([])
    with BrowserSession(cdp_port=9264) as s:
        page = s.goto(f"{fixture_server}/otp.html")
        out = la.clear_otp(
            page, "#code", CREDS, OtpInbox(server.url, "a", "c", backoff_s=0, retries=1),
            human_entry_possible=False, timeout_s=1.5, poll_s=0.2, markers=["/otp"],
        )
    assert not out.cleared and la.otp_error_class(out) == "mfa_timeout"


def test_dismiss_overlays_rejects_rather_than_accepts_and_reports_the_click(fixture_server) -> None:
    with BrowserSession(cdp_port=9265) as s:
        page = s.goto(f"{fixture_server}/login-consent.html")
        clicked = la.dismiss_overlays(page)
        consent = page.evaluate("() => document.body.dataset.consent")
        banner_left = page.locator("#cookie-banner").count()
    assert consent == "reject-all" and banner_left == 0
    assert len(clicked) == 1 and "reject" in clicked[0].lower()


def test_resolve_unique_narrows_a_hidden_twin_to_the_visible_button(fixture_server) -> None:
    with BrowserSession(cdp_port=9266) as s:
        page = s.goto(f"{fixture_server}/login.html")
        found = la.resolve_unique(page, 'button:has-text("Sign in")')
        count = page.locator(found.selector).count() if found.selector else 0
    assert found.error is None and "visible=true" in found.selector and count == 1


def test_resolve_unique_reports_not_found_and_not_unique_honestly(fixture_server) -> None:
    with BrowserSession(cdp_port=9267) as s:
        page = s.goto(f"{fixture_server}/login.html")
        missing = la.resolve_unique(page, "#nope", timeout_ms=500)
        ambiguous = la.resolve_unique(page, "input", timeout_ms=500)  # three visible inputs
    assert missing.error == "not_found"
    assert ambiguous.error == "not_unique"


def test_ensure_enabled_waits_for_a_submit_the_banner_had_disabled(fixture_server) -> None:
    with BrowserSession(cdp_port=9268) as s:
        page = s.goto(f"{fixture_server}/login-consent.html")
        submit = page.locator("#submit-visible")
        assert not la.ensure_enabled(page, submit, budget_s=0.5)  # still under the banner
        la.dismiss_overlays(page)
        assert la.ensure_enabled(page, submit)


def test_settle_after_click_tells_a_navigation_from_a_rejection(fixture_server) -> None:
    with BrowserSession(cdp_port=9269) as s:
        page = s.goto(f"{fixture_server}/login.html")
        la.fill_credential(page, "#username", LOGIN_EMAIL, CREDS)
        la.fill_credential(page, "#password", LOGIN_PASSWORD, CarrierCreds(slug="pie", login_url="x", password="wrong"))
        before = page.url
        page.click("#submit-visible")
        rejected = la.settle_after_click(page, before, "#submit-visible", settle_s=1)
        la.fill_credential(page, "#password", LOGIN_PASSWORD, CREDS)
        page.click("#submit-visible")
        moved = la.settle_after_click(page, before, "#submit-visible", settle_s=3)
    assert rejected is False and moved is True
