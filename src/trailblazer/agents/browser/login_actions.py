"""What a FormFiller needs to act on a login page. Functions, not an agent.

The FormFiller is another team's agent and does not exist yet. When it does, a
login page asks four things of it that a form page does not, and this module
answers each with one function so the agent's owner can call them rather than
rediscover them on a carrier:

- `fill_credential`: type LOGIN_EMAIL or LOGIN_PASSWORD from the carrier's
  credentials. The caller reports the key, never the value.
- `clear_otp`: resolve LOGIN_OTP by pulling the code from the inbox and
  clearing the challenge (the toolkit types and submits; see `mfa.py`).
- `dismiss_overlays`: answer a consent banner before acting, most
  privacy-preserving choice first, and say what was clicked so the walk records it.
- `resolve_unique`: turn a selector that matches a hidden look-alike (Auth0's
  twin submit buttons) into the one visible node a person would click.

Plus two small ones every login click needs: `ensure_enabled` (a submit that
validates on blur stays disabled while the password field has focus) and
`settle_after_click` (did the click move the page?). Everything here takes a
Playwright `Page`; nothing knows about Frontier, Loop, or the FillReport.
"""

import logging
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from trailblazer.agents.browser.mfa import OtpWait, wait_for_otp_clear
from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.contracts import LOGIN_EMAIL, LOGIN_OTP, LOGIN_PASSWORD
from trailblazer.shared.carrier_creds import CarrierCreds

log = logging.getLogger(__name__)

# Consent-banner and overlay dismissers, most privacy-preserving first. Only ever
# clicked INSIDE something that looks like a banner or dialog, so a form's own
# "Decline" (coverage) button is never touched.
OVERLAY_CONTAINERS = (
    '[role="dialog"], [aria-modal="true"], [id*="cookie" i], [class*="cookie" i], '
    '[id*="consent" i], [class*="consent" i], [class*="banner" i], [id*="banner" i]'
)
OVERLAY_DISMISSERS = [
    "reject all",
    "reject",
    "decline",
    "deny",
    "necessary only",
    "essential only",
    "only necessary",
    "save settings",
    "close",
    "got it",
    "dismiss",
    "no thanks",
    "accept all",
    "accept",
    "ok",
]


@dataclass
class Resolved:
    """One element to act on, or the reason there is not one."""

    locator: Locator | None
    error: str | None
    """`not_found` | `not_unique` | None, in FillReport.errorClass vocabulary."""
    selector: str
    """The selector that resolves to exactly the element, for the walk to record."""


def resolve_unique(
    page: Page,
    selector: str,
    *,
    credential_anchor: str | None = None,
    timeout_ms: int = 5000,
) -> Resolved:
    """Exactly one element for `selector`, narrowing a hidden look-alike to the visible one.

    Hosted identity providers render two "Continue" buttons in two forms, one
    hidden. A selector matching both is narrowed to `>> visible=true`; if that
    still leaves two, `credential_anchor` (the locator of the credential just
    filled) picks the one in the form that owns it, so the right form is
    submitted and never the empty twin.
    """
    loc = page.locator(selector)
    try:
        loc.first.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightError:
        return Resolved(None, "not_found", selector)
    count = loc.count()
    if count == 1:
        return Resolved(loc.first, None, selector)
    if count == 0:
        return Resolved(None, "not_found", selector)
    visible_sel = f"{selector} >> visible=true"
    visible = page.locator(visible_sel)
    if visible.count() == 1:
        log.info("%r matched %d nodes, 1 visible; using it", selector, count)
        return Resolved(visible.first, None, visible_sel)
    if credential_anchor:
        scoped_sel = f"form:has({credential_anchor}) {selector} >> visible=true"
        scoped = page.locator(scoped_sel)
        if scoped.count() >= 1:
            log.info("%r scoped to the form holding %s", selector, credential_anchor)
            return Resolved(scoped.first, None, f"{scoped_sel} >> nth=0")
    return Resolved(None, "not_unique", selector)


def ensure_enabled(page: Page, loc: Locator, budget_s: float = 3.0) -> bool:
    """Blur, then wait for a disabled control to enable. False if it never does."""
    try:
        if not loc.is_disabled():
            return True
        page.keyboard.press("Tab")
        waited = 0.0
        while loc.is_disabled():
            if waited >= budget_s:
                return False
            page.wait_for_timeout(200)
            waited += 0.2
        return True
    except PlaywrightError:
        return True


def dismiss_overlays(page: Page, budget_s: float = 2.0) -> list[str]:
    """Close consent banners and dialogs. Returns the selectors clicked, in order.

    Most privacy-preserving choice first. Each click belongs in the walk: a
    replay that does not know about the banner fails on a disabled submit or an
    intercepted click.
    """
    clicked: list[str] = []
    spent = 0.0
    while spent <= budget_s:
        hit = False
        for word in OVERLAY_DISMISSERS:
            sel = f'{OVERLAY_CONTAINERS} >> :is(button, a, [role="button"]):has-text("{word}") >> visible=true'
            try:
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                loc.first.click(timeout=2000)
                page.wait_for_timeout(300)
                spent += 0.3
                clicked.append(sel)
                log.info("dismissed an overlay via %r", word)
                hit = True
                break
            except PlaywrightError:
                continue
        if not hit:
            break
    return clicked


def credential_value(creds: CarrierCreds | None, key: str) -> str | None:
    """The secret for LOGIN_EMAIL or LOGIN_PASSWORD, or None when not on file."""
    if creds is None:
        return None
    if key == LOGIN_EMAIL:
        return creds.username
    if key == LOGIN_PASSWORD:
        return creds.password
    return None


@dataclass
class FillOutcome:
    ok: bool
    selector: str
    error: str | None = None
    """`auth` when the credential is not on file, `widget` when the field refused the value."""


def fill_credential(
    page: Page,
    selector: str,
    key: str,
    creds: CarrierCreds | None,
    *,
    timeout_ms: int = 5000,
) -> FillOutcome:
    """Type a credential into the control at `selector`, verify it landed, refill once.

    The caller records `key` as what was typed. The value never leaves this function.
    """
    value = credential_value(creds, key)
    if value is None:
        log.error("no %s on file for this carrier", key)
        return FillOutcome(False, selector, "auth")
    found = resolve_unique(page, selector, timeout_ms=timeout_ms)
    if found.error:
        return FillOutcome(False, found.selector, found.error)
    loc = found.locator
    loc.fill(value, timeout=timeout_ms)
    if loc.input_value() != value:
        # A React-controlled input can drop a programmatic value; once more, then give up.
        loc.fill(value, timeout=timeout_ms)
        if loc.input_value() != value:
            log.error("%s did not accept the credential", found.selector)
            return FillOutcome(False, found.selector, "widget")
    return FillOutcome(True, found.selector)


def clear_otp(
    page: Page,
    selector: str | None,
    creds: CarrierCreds | None,
    inbox: OtpInbox | None,
    *,
    human_entry_possible: bool,
    timeout_s: float = 600.0,
    poll_s: float = 2.0,
    settle_s: float = 12.0,
    markers: list[str] | None = None,
) -> OtpWait:
    """Resolve LOGIN_OTP: pull the code, type it, submit, wait for the challenge to clear.

    The inbox is keyed by the carrier's slug when its MFA is on. Six
    single-character boxes are detected here and filled one character each.
    Returns the toolkit's `OtpWait`; the caller maps it to a report.
    """
    slug = creds.mfa_carrier_id if creds else None
    per_digit = page.locator('input[maxlength="1"]').count() >= 4
    return wait_for_otp_clear(
        page,
        inbox=inbox,
        carrier_slug=slug,
        timeout_s=timeout_s,
        poll_s=poll_s,
        settle_s=settle_s,
        markers=markers,
        otp_selector=None if per_digit else selector,
        per_digit=per_digit,
        human_entry_possible=human_entry_possible,
    )


def otp_error_class(out: OtpWait) -> str:
    """`mfa_timeout` when the wait ran out, `auth` when no code could ever be obtained."""
    return "mfa_timeout" if out.reason.startswith("timed out") else "auth"


def settle_after_click(page: Page, url_before: str, selector: str, settle_s: float = 3.0) -> bool:
    """Did the click move the page? Wait for the network to go quiet, then compare."""
    try:
        page.wait_for_load_state("networkidle", timeout=int(settle_s * 1000))
    except PlaywrightError:
        pass
    if page.url != url_before:
        return True
    try:
        return page.locator(selector).count() == 0
    except PlaywrightError:
        return True


def is_credential_key(key: str | None) -> bool:
    return key in (LOGIN_EMAIL, LOGIN_PASSWORD, LOGIN_OTP)
