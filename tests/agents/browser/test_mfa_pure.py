"""The MFA helpers that need no browser: URL judgement and the delivery-screen guard.

The fake page below mirrors the stub Roadrunner's vitest suite used, so the
same cases hold here: a code screen dispatches nothing, a live send control is
clicked, an inert one is `blocked` (the Chubb failure that used to be a silent
ten-minute timeout), and a screen with no send control is `unknown`.
"""

import re

import pytest

from trailblazer.agents.browser.mfa import (
    ensure_code_requested,
    is_auth_callback_url,
    is_pre_portal_url,
    on_phone_challenge,
    prefer_email_channel,
    try_auto_fill_otp,
    wait_for_otp_clear,
)


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://portal.example/login",
        "https://idp.example/u/mfa-sms-challenge?state=x",
        "https://portal.example/verify",
        "https://portal.example/account/two-factor",
    ],
)
def test_login_and_challenge_urls_are_pre_portal(url: str) -> None:
    assert is_pre_portal_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://portal.example/login/callback?code=abc&state=xyz",
        "https://portal.example/callback?code=abc",
        "https://portal.example/auth/return?code=abc&state=xyz",
    ],
)
def test_an_oidc_callback_is_the_success_hop_not_a_login_page(url: str) -> None:
    assert is_auth_callback_url(url)
    assert not is_pre_portal_url(url)


def test_a_plain_code_query_param_is_not_a_callback() -> None:
    assert not is_auth_callback_url("https://portal.example/quote?code=PROMO")


def test_markers_are_configurable() -> None:
    assert not is_pre_portal_url("https://portal.example/dashboard")
    assert is_pre_portal_url("https://portal.example/dashboard", markers=["/dash"])
    assert not is_pre_portal_url("https://portal.example/login", markers=["/otp"])


# --------------------------------------------------------------------------- #
# A fake page: only the surface mfa.py touches.
# --------------------------------------------------------------------------- #


class FakeLocator:
    def __init__(self, page, matches: list["FakeElement"]):
        self.page = page
        self.matches = matches

    @property
    def first(self):
        return FakeLocator(self.page, self.matches[:1])

    def nth(self, i):
        return FakeLocator(self.page, self.matches[i : i + 1])

    def count(self):
        return len(self.matches)

    def is_visible(self, **_):
        return bool(self.matches) and self.matches[0].visible

    def is_enabled(self, **_):
        return bool(self.matches) and self.matches[0].enabled

    def click(self, **_):
        self.page.clicks.append(self.matches[0].name)
        if self.matches[0].on_click:
            self.matches[0].on_click(self.page)

    def fill(self, value, **_):
        self.matches[0].value = value
        self.page.fills.append((self.matches[0].name, value))

    def inner_text(self, **_):
        return self.page.text


class FakeElement:
    def __init__(self, name, *, role="button", visible=True, enabled=True, kind="button", on_click=None):
        self.name = name
        self.role = role
        self.visible = visible
        self.enabled = enabled
        self.kind = kind  # "button" | "otp" | "digit"
        self.on_click = on_click
        self.value = ""


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.keys.append(key)


class FakePage:
    def __init__(self, elements: list[FakeElement], *, url="https://carrier.example/verify", text=""):
        self.elements = elements
        self.url = url
        self.text = text
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.keys: list[str] = []
        self.keyboard = FakeKeyboard(self)

    def wait_for_timeout(self, ms):
        pass

    def locator(self, sel):
        if sel == "body":
            return FakeLocator(self, [FakeElement("body")])
        if re.search(r'maxlength="1"', sel):
            return FakeLocator(self, [e for e in self.elements if e.kind == "digit"])
        if re.search(r"otp|code|numeric|one-time", sel, re.I) and sel.startswith("input"):
            return FakeLocator(self, [e for e in self.elements if e.kind == "otp"])
        if sel.startswith("button") or sel.startswith(":is("):
            m = re.search(r'has-text\("([^"]+)"\)', sel)
            if m:
                return FakeLocator(
                    self, [e for e in self.elements if e.kind == "button" and m.group(1).lower() in e.name.lower()]
                )
            if 'type="submit"' in sel:
                return FakeLocator(self, [e for e in self.elements if e.kind == "button" and e.role == "submit"])
        return FakeLocator(self, [])

    def get_by_role(self, role, name=None):
        found = [
            e for e in self.elements if e.kind == "button" and e.role == role and (name is None or name.search(e.name))
        ]
        return FakeLocator(self, found)


# --------------------------------------------------------------------------- #
# ensure_code_requested
# --------------------------------------------------------------------------- #


def test_code_screen_dispatches_nothing() -> None:
    page = FakePage([FakeElement("code", kind="otp"), FakeElement("Next")])
    assert ensure_code_requested(page) == "code-screen"
    assert page.clicks == []


def test_a_live_send_control_is_clicked() -> None:
    page = FakePage([FakeElement("Send code")])
    assert ensure_code_requested(page) == "requested"
    assert page.clicks == ["Send code"]


def test_an_inert_send_control_is_blocked_the_chubb_failure() -> None:
    page = FakePage([FakeElement("Next", enabled=False)])
    assert ensure_code_requested(page) == "blocked"
    assert page.clicks == []


def test_choosing_email_can_wake_an_inert_send_control() -> None:
    """The radio card is inert; picking its inner label enables Next."""
    nxt = FakeElement("Next", enabled=False)

    def enable_next(page):
        nxt.enabled = True

    page = FakePage([FakeElement("Receive an email", role="radio", on_click=enable_next), nxt])
    assert ensure_code_requested(page) == "requested"
    assert page.clicks == ["Receive an email", "Next"]


def test_no_send_control_is_unknown() -> None:
    assert ensure_code_requested(FakePage([])) == "unknown"


# --------------------------------------------------------------------------- #
# prefer_email_channel
# --------------------------------------------------------------------------- #


def test_a_phone_challenge_is_steered_to_email() -> None:
    page = FakePage(
        [FakeElement("Try another method", role="link"), FakeElement("Email", role="radio"), FakeElement("Send code")],
        text="We sent a text message with a code to your phone ending 1234",
    )
    assert on_phone_challenge(page)
    assert prefer_email_channel(page) is True
    assert page.clicks == ["Try another method", "Email", "Send code"]


def test_an_email_default_screen_is_left_alone() -> None:
    page = FakePage([FakeElement("Send code")], text="We emailed a code to a***@aidenrisk.com")
    assert prefer_email_channel(page) is False
    assert page.clicks == []


def test_a_phone_challenge_with_no_email_option_is_reported_not_forced() -> None:
    page = FakePage([FakeElement("Send code")], url="https://idp.example/u/mfa-sms-challenge")
    assert prefer_email_channel(page) is False


# --------------------------------------------------------------------------- #
# try_auto_fill_otp
# --------------------------------------------------------------------------- #


def test_single_input_is_filled_and_submitted() -> None:
    page = FakePage([FakeElement("code", kind="otp"), FakeElement("Verify")])
    assert try_auto_fill_otp(page, "123456")
    assert page.fills == [("code", "123456")] and page.clicks == ["Verify"]


def test_per_digit_boxes_are_filled_one_character_each() -> None:
    page = FakePage([FakeElement(f"d{i}", kind="digit") for i in range(6)])
    assert try_auto_fill_otp(page, "123456", per_digit=True)
    assert [v for _, v in page.fills] == list("123456") and page.keys == ["Enter"]


def test_nothing_to_fill_is_reported_honestly() -> None:
    assert try_auto_fill_otp(FakePage([]), "123456") is False


# --------------------------------------------------------------------------- #
# wait_for_otp_clear: the fast-fail paths need no browser either.
# --------------------------------------------------------------------------- #


def test_headless_with_no_inbox_fails_at_once() -> None:
    out = wait_for_otp_clear(FakePage([]), inbox=None, human_entry_possible=False, timeout_s=5)
    assert not out.cleared and "no inbox" in out.reason and out.polls == 0


def test_an_inert_delivery_screen_fails_at_once_instead_of_waiting() -> None:
    class Inbox:
        disabled = False

        def fetch(self, slug):
            return None

    page = FakePage([FakeElement("Next", enabled=False)])
    out = wait_for_otp_clear(page, inbox=Inbox(), carrier_slug="chubb", timeout_s=5)
    assert not out.cleared and out.dispatch == "blocked" and out.polls == 0
