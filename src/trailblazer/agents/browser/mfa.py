"""Clearing a one-time-code challenge on a live page.

A port of Roadrunner's `src/lib/mfa-otp.js`, itself a port of quote-engine's
Python, with the lessons that accumulated since folded in:

- The URL is a lying proxy. An OIDC callback contains `/login` and is the
  success hop; Auth0 stays on `/u/login` through the whole challenge. Detection
  is by content where it can be, and the URL markers are configurable.
- The inbox only ever sees email. A challenge delivering to a phone is steered
  to email first, and an emailed code is never typed into a phone challenge.
- A code nobody requested never arrives. When the delivery screen's send
  control is inert the wait fails at once instead of ten minutes later.
- One challenge has one valid code. Stale codes queued by earlier runs are
  drained, but the newest is kept and tried first; draining everything would
  deadlock a single-code inbox.
- A timeout says why: never steered, inbox empty, or codes arrived and the
  fill did not take.

Used by FormFiller at capture time (resolving `LOGIN_OTP`) and by Validator's
runner at replay time. Nothing here knows about Frontier or Loop. Everything
here takes a Playwright `Page` and is duck-typed enough to run against a fake.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

log = logging.getLogger(__name__)

# URL fragments that mean "still on a login / code / device-trust screen".
DEFAULT_PRE_PORTAL_MARKERS: tuple[str, ...] = (
    "/login",
    "/otp",
    "/verify",
    "/code",
    "/mfa",
    "/two-factor",
    "/2fa",
    "/auth",
)

# Single-input code fields, generic fallbacks after any recorded selector.
DEFAULT_SINGLE_SELECTORS = [
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[name*="code" i]',
    'input[id*="otp" i]',
    'input[id*="code" i]',
    'input[inputmode="numeric"]',
]

DEFAULT_SUBMIT_SELECTORS = [
    'button:has-text("Verify")',
    'button:has-text("Continue")',
    'button:has-text("Submit")',
    'button:has-text("Sign in")',
    'button[type="submit"]',
]

# Language that means the code is going to a phone.
SMS_DELIVERY_HINTS = [
    "text message",
    "sms",
    "we texted",
    "sent a text",
    "to your phone",
    "to your mobile",
    "message with a code",
    "code to your phone",
    "code via text",
]
# Controls that reveal the other delivery methods. Substrings, so the leading
# verb does not matter: "try another method" and "choose another method" both match.
CHANNEL_SWITCH_TRIGGERS = [
    "another method",
    "different method",
    "another way",
    "different way",
    "other ways to verify",
    "more options",
    "didn't receive",
    "didn't get",
]
# The email option among the choices, specific first, bare "email" last.
EMAIL_OPTION_PATTERNS = [
    "send code to email",
    "send to email",
    "verify by email",
    "email me",
    "via email",
    "by email",
    "use email",
    "email address",
    "email",
]
# What dispatches the code once email is chosen.
SEND_CODE_TRIGGERS = ["send code", "email code", "send email", "send a code", "send", "continue", "next"]

# Path only: the query string carries an opaque state blob that can contain these letters.
_SMS_URL_HINTS = re.compile(r"sms|phone|voice|text-?challenge", re.IGNORECASE)
_CALLBACK_PATH = re.compile(r"/(?:login/)?callback(?:[/?#]|$)", re.IGNORECASE)
_CODE_PARAM = re.compile(r"[?&]code=")
_STATE_PARAM = re.compile(r"[?&]state=")

Dispatch = Literal["code-screen", "requested", "blocked", "unknown"]


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #


def is_auth_callback_url(url: str) -> bool:
    """An OIDC redirect callback: the SUCCESS hop, not a login surface.

    Auth0 lands on `<portal>/login/callback?code=…&state=…`, which contains
    `/login` and so matched the pre-portal markers; a cleared challenge then
    looked uncleared, and the waiter polled to its timeout pulling codes it did
    not need.
    """
    u = str(url or "")
    if not _CODE_PARAM.search(u):
        return False
    return bool(_CALLBACK_PATH.search(u)) or bool(_STATE_PARAM.search(u))


def is_pre_portal_url(url: str, markers: tuple[str, ...] | list[str] | None = None) -> bool:
    """Still on a sign-in, code, or device-trust screen, judged by the URL."""
    u = str(url or "").lower()
    if is_auth_callback_url(u):
        return False
    return any(m in u for m in (markers or DEFAULT_PRE_PORTAL_MARKERS))


def _path_hints_phone(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        return bool(_SMS_URL_HINTS.search(urlparse(url).path))
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Page probes. Every one swallows Playwright errors: a page mid-navigation is
# a normal state here, never a reason to fail the wait.
# --------------------------------------------------------------------------- #


def _visible(loc: Any) -> bool:
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _enabled(loc: Any) -> bool:
    try:
        return bool(loc.first.is_enabled())
    except Exception:
        return False


def _page_text(page: Any) -> str:
    try:
        return (page.locator("body").inner_text(timeout=5000) or "").lower()
    except Exception:
        return ""


def page_mentions(page: Any, patterns: list[str]) -> bool:
    text = _page_text(page)
    return any(p in text for p in patterns)


def on_phone_challenge(page: Any) -> bool:
    """Is the code being delivered to a phone? Judged by URL path, then page text."""
    return _path_hints_phone(page.url) or page_mentions(page, SMS_DELIVERY_HINTS)


def click_first_match(page: Any, patterns: list[str], label: str) -> bool:
    """Click the first visible clickable element matching any pattern.

    Restricted to clickable roles and tags so matching stray label text never
    fires a navigation.
    """
    for pat in patterns:
        rx = re.compile(re.escape(pat), re.IGNORECASE)
        for role in ("button", "link", "radio", "tab", "menuitem", "option"):
            try:
                loc = page.get_by_role(role, name=rx)
                if _visible(loc):
                    loc.first.click(timeout=2000)
                    log.info("mfa %s: %r via %s", label, pat, role)
                    return True
            except Exception:
                continue
        try:
            loc = page.locator(f':is(button,a,[role="button"],[role="radio"],label,li):has-text("{pat}")')
            if _visible(loc):
                loc.first.click(timeout=2000)
                log.info("mfa %s: %r via text", label, pat)
                return True
        except Exception:
            continue
    return False


def prefer_email_channel(page: Any) -> bool:
    """If the challenge is delivering to a phone, steer it to email.

    Returns True when the channel was switched; False when email is already the
    default or the screen offers no email option.
    """
    if not on_phone_challenge(page):
        return False
    log.info("mfa: phone delivery detected, steering to email")
    click_first_match(page, CHANNEL_SWITCH_TRIGGERS, "switch-trigger")
    page.wait_for_timeout(800)
    if not click_first_match(page, EMAIL_OPTION_PATTERNS, "email-option"):
        log.warning("mfa: no email delivery option found; leaving the channel as is")
        return False
    page.wait_for_timeout(500)
    click_first_match(page, SEND_CODE_TRIGGERS, "send-code")
    log.info("mfa: requested the code by email")
    return True


def otp_input_present(page: Any, otp_selector: str | None = None) -> bool:
    """Is a code input on screen? Content, not URL, is the fact."""
    selectors = ([otp_selector] if otp_selector else []) + DEFAULT_SINGLE_SELECTORS
    for sel in selectors:
        try:
            if _visible(page.locator(sel)):
                return True
        except Exception:
            continue
    try:
        return page.locator('input[maxlength="1"]').count() >= 4
    except Exception:
        return False


def _send_control(page: Any) -> Any | None:
    """The control that dispatches the code on a delivery-choice screen."""
    for pat in SEND_CODE_TRIGGERS:
        try:
            loc = page.get_by_role("button", name=re.compile(re.escape(pat), re.IGNORECASE))
            if _visible(loc):
                return loc
        except Exception:
            continue
    return None


def ensure_code_requested(page: Any, otp_selector: str | None = None) -> Dispatch:
    """Make sure a code was actually asked for before waiting for one.

    `code-screen`: the code input is already up, nothing to dispatch.
    `requested`: a live send control was clicked.
    `blocked`: the send control stayed inert even after choosing email; no code
    can arrive, so the caller should stop now rather than wait.
    `unknown`: no send control on this screen; proceed and let the wait judge.
    """
    if otp_input_present(page, otp_selector):
        return "code-screen"
    send = _send_control(page)
    if send is None:
        return "unknown"
    if not _enabled(send):
        # An inert dispatch control means the delivery choice never registered
        # (Chubb's radio card is inert; its inner label is the control). Choose
        # email explicitly and look again.
        click_first_match(page, EMAIL_OPTION_PATTERNS, "email-option")
        page.wait_for_timeout(600)
    ready = _send_control(page)
    if ready is None or not _enabled(ready):
        return "blocked"
    try:
        ready.first.click(timeout=3000)
    except Exception:
        return "blocked"
    log.info("mfa: dispatched a code from the delivery screen")
    return "requested"


def try_auto_fill_otp(
    page: Any,
    code: str,
    *,
    otp_selector: str | None = None,
    submit_selector: str | None = None,
    per_digit: bool = False,
) -> bool:
    """Type the code into the code input and submit. True when both happened.

    The recorded selector is tried first, generic fallbacks second. Six
    single-character boxes are filled one character at a time. The code itself
    is never logged.
    """
    if not per_digit:
        singles = ([otp_selector] if otp_selector else []) + DEFAULT_SINGLE_SELECTORS
        for sel in singles:
            try:
                field_loc = page.locator(sel)
                if not _visible(field_loc):
                    continue
                field_loc.first.fill(code)
                log.info("mfa: filled the code via %s", sel)
                submits = ([submit_selector] if submit_selector else []) + DEFAULT_SUBMIT_SELECTORS
                for sub in submits:
                    try:
                        btn = page.locator(sub)
                        if _visible(btn):
                            btn.first.click()
                            log.info("mfa: submitted via %s", sub)
                            return True
                    except Exception:
                        continue
                page.keyboard.press("Enter")
                return True
            except Exception:
                continue
    try:
        digits = page.locator('input[maxlength="1"]')
        n = digits.count()
        if n >= 4 and n == len(code):
            for i, ch in enumerate(code):
                digits.nth(i).fill(ch)
            log.info("mfa: filled %d digit boxes", n)
            page.keyboard.press("Enter")
            return True
    except Exception:
        pass
    log.warning("mfa: could not find a code input to fill")
    return False


def replay_channel_switch(page: Any, selectors: list[str]) -> None:
    """Replay the exact clicks a capture recorded for switching to email, best effort."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if _visible(loc):
                loc.first.click(timeout=2000)
                log.info("mfa: switch step %s", sel)
                page.wait_for_timeout(600)
        except Exception:
            continue


# --------------------------------------------------------------------------- #
# The wait
# --------------------------------------------------------------------------- #


@dataclass
class OtpWait:
    """What happened while waiting for the challenge to clear."""

    cleared: bool
    reason: str
    final_url: str = ""
    polls: int = 0
    fetches: int = 0
    codes: int = 0
    attempts: int = 0
    phone_blocked: int = 0
    capped: int = 0
    drained: int = 0
    steered: bool = False
    dispatch: Dispatch | str = "n/a"
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"polls={self.polls} inboxFetches={self.fetches} codesReceived={self.codes} "
            f"fillAttempts={self.attempts} phoneBlockedPolls={self.phone_blocked} "
            f"pollsAfterCap={self.capped} steeredToEmail={self.steered} dispatch={self.dispatch} "
            f"stalesDrained={self.drained} lastUrl={self.final_url}"
        )


HANDOFF_INSTRUCTION = "Type the one-time code into the visible browser window."


def wait_for_otp_clear(
    page: Any,
    *,
    inbox: Any = None,
    carrier_slug: str | None = None,
    timeout_s: float = 600.0,
    poll_s: float = 2.0,
    settle_s: float = 12.0,
    max_attempts: int = 3,
    markers: tuple[str, ...] | list[str] | None = None,
    otp_selector: str | None = None,
    submit_selector: str | None = None,
    per_digit: bool = False,
    channel_switch: list[str] | None = None,
    human_entry_possible: bool = True,
    prefer_email: bool = True,
    on_handoff: Callable[[str], None] | None = None,
    clock: Any = time,
) -> OtpWait:
    """Poll until the page leaves the challenge, filling codes from the source as they arrive.

    `inbox` is any code source with `fetch(slug)`: the shared email inbox, an
    authenticator seed, or an operator's file drop (see `code_sources.py`).
    With a source and a slug, codes are claimed and typed (at most
    `max_attempts`, with `settle_s` between, since a good code takes the portal
    a few seconds to redirect). Without them, the wait is for a person typing
    the code in a visible browser, and `on_handoff` is told once what that
    person must do; when no person can (headless), it fails at once. Returns an
    `OtpWait` and never raises: a timeout is an answer.
    """
    out = OtpWait(cleared=False, reason="", final_url=str(page.url))
    can_auto_pull = bool(inbox is not None and carrier_slug and not getattr(inbox, "disabled", False))
    if not can_auto_pull and not human_entry_possible:
        out.reason = "no way to obtain a code: no inbox configured and no visible browser for a person"
        log.error("mfa: %s", out.reason)
        return out
    if can_auto_pull:
        log.info("mfa: on a code screen; pulling codes for %r, a person is the fallback", carrier_slug)
    else:
        log.warning("mfa: on a code screen with no code source; a person must type it. %s", HANDOFF_INSTRUCTION)
        if on_handoff is not None:
            try:
                on_handoff(HANDOFF_INSTRUCTION)
            except Exception as e:  # a UI hook must never take the run down
                log.warning("mfa: on_handoff hook failed: %s", e)
    # An authenticator seed always has a code and never a backlog; steering the
    # channel to email would be wrong for it too.
    queued_source = bool(getattr(inbox, "queued", True))
    if can_auto_pull and not queued_source:
        prefer_email = False

    def steer() -> None:
        if not prefer_email or out.steered:
            return
        try:
            if channel_switch:
                replay_channel_switch(page, channel_switch)
                out.steered = True
            else:
                out.steered = prefer_email_channel(page)
        except Exception as e:
            log.warning("mfa: channel switch skipped: %s", e)

    steer()
    try:
        out.dispatch = ensure_code_requested(page, otp_selector)
    except Exception:
        out.dispatch = "unknown"
    if out.dispatch == "blocked":
        out.reason = "the delivery screen will not dispatch a code (its send control stayed inert)"
        log.error("mfa: %s", out.reason)
        return out

    # One challenge has one valid code: keep the newest queued one, drop the rest.
    carried: str | None = None
    if can_auto_pull and queued_source:
        for _ in range(5):
            queued = inbox.fetch(carrier_slug)  # type: ignore[union-attr]
            if not queued:
                break
            if carried:
                out.drained += 1
            carried = queued
        if out.drained:
            log.info("mfa: %d queued code(s) discarded as stale; keeping the newest", out.drained)

    deadline = clock.monotonic() + timeout_s
    last_url = str(page.url)
    last_attempt_at: float | None = None
    while clock.monotonic() < deadline:
        page.wait_for_timeout(int(poll_s * 1000))
        out.polls += 1
        current = str(page.url)
        if current != last_url:
            log.info("mfa: navigated %s -> %s", last_url, current)
            last_url = current
        out.final_url = current
        if not is_pre_portal_url(current, markers):
            out.cleared = True
            out.reason = f"cleared, landed on {current}"
            log.info("mfa: %s", out.reason)
            return out
        if not can_auto_pull:
            continue
        steer()
        # Our inbox only holds emailed codes; typing one into a phone challenge burns it.
        if not out.steered and on_phone_challenge(page):
            out.phone_blocked += 1
            if out.phone_blocked == 1 or out.phone_blocked % 30 == 0:
                log.info("mfa: still on a phone challenge (%d polls), not spending an emailed code", out.phone_blocked)
            continue
        if last_attempt_at is not None and clock.monotonic() - last_attempt_at < settle_s:
            continue
        if out.attempts >= max_attempts:
            out.capped += 1
            continue
        out.fetches += 1
        code = carried or inbox.fetch(carrier_slug)  # type: ignore[union-attr]
        carried = None
        if code:
            out.codes += 1
            out.attempts += 1
            last_attempt_at = clock.monotonic()
            log.info("mfa: code received, fill attempt %d/%d", out.attempts, max_attempts)
            try_auto_fill_otp(
                page, code, otp_selector=otp_selector, submit_selector=submit_selector, per_digit=per_digit
            )

    if not out.steered and can_auto_pull and out.phone_blocked:
        why = "never switched the challenge to email"
    elif out.codes == 0:
        why = "inbox never returned a code" if can_auto_pull else "nobody entered a code"
    else:
        why = "codes arrived but the page never left the code screen (wrong or expired code, or the fill failed)"
    out.reason = f"timed out after {int(timeout_s)}s: {why}"
    log.error("mfa: %s; %s", out.reason, out.summary())
    return out
