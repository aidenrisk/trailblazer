"""Clearing a code screen end to end: real browser, fixture portal, scripted inbox.

`otp.html` accepts 123456 and lands on `dashboard.html`; anything else stays on
the code screen and shows an error. The fake inbox plays the backend.
"""

from trailblazer.agents.browser.mfa import wait_for_otp_clear
from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.agents.browser.session import BrowserSession

MARKERS = ["/otp"]  # the fixture's own URL shape; the defaults are tuned to carriers


def _wait(page, inbox_url, **kw):
    inbox = OtpInbox(inbox_url, "a", "c", backoff_s=0, retries=1)
    return wait_for_otp_clear(
        page,
        inbox=inbox,
        carrier_slug="pie",
        markers=MARKERS,
        poll_s=0.2,
        settle_s=0.5,
        **kw,
    )


def test_the_code_arrives_late_and_the_challenge_clears(fixture_server, fake_inbox) -> None:
    server = fake_inbox([(204, None), (204, None), (200, {"code": "123456"})])

    with BrowserSession(cdp_port=9241) as session:
        page = session.goto(f"{fixture_server}/otp.html")
        out = _wait(page, server.url, timeout_s=20)

    assert out.cleared, out.summary()
    assert out.final_url.endswith("/dashboard.html")
    assert out.codes == 1 and out.attempts == 1
    assert out.dispatch == "code-screen"  # the input was already up; nothing was dispatched


def test_stale_queued_codes_are_drained_and_the_newest_is_tried_first(fixture_server, fake_inbox) -> None:
    """Two runs ago left 000000 behind; the newest, 123456, is the live one."""
    server = fake_inbox([(200, {"code": "000000"}), (200, {"code": "123456"}), (204, None)])

    with BrowserSession(cdp_port=9242) as session:
        page = session.goto(f"{fixture_server}/otp.html")
        out = _wait(page, server.url, timeout_s=20)

    assert out.cleared, out.summary()
    assert out.drained == 1
    assert out.attempts == 1  # the stale code was never typed


def test_wrong_codes_are_capped_and_the_timeout_says_why(fixture_server, fake_inbox) -> None:
    server = fake_inbox([(200, {"code": "999999"})] * 6)

    with BrowserSession(cdp_port=9243) as session:
        page = session.goto(f"{fixture_server}/otp.html")
        out = _wait(page, server.url, timeout_s=4, max_attempts=2)

    assert not out.cleared
    assert out.attempts == 2 and out.capped >= 1
    assert "codes arrived but the page never left" in out.reason
    assert out.final_url.endswith("/otp.html")


def test_an_empty_inbox_times_out_naming_the_inbox(fixture_server, fake_inbox) -> None:
    server = fake_inbox([])  # every call 204

    with BrowserSession(cdp_port=9244) as session:
        page = session.goto(f"{fixture_server}/otp.html")
        out = _wait(page, server.url, timeout_s=2)

    assert not out.cleared
    assert "inbox never returned a code" in out.reason
    assert out.fetches >= 3
