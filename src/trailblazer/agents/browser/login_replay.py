"""Replay a captured login prefix on a live tab, deterministically. Not an agent.

`Walk.login` is the list of steps the chain took to get a tab authenticated:
type a credential by key, choose the email channel, click Sign in, type the
code. This module runs that list again in a fresh browser with no model, which
is what makes a captured login trustworthy ("a recipe is trustworthy only after
it has driven a login itself"), what a health check is, and what a warm run
does before the form walk when a saved session did not hold.

It is login-only by construction: it knows credential keys, code screens,
consent banners, and the one-click-then-wait shape of a sign-in. It is not a
Program runner; ReplayGen and Validator belong to their owners, and the notes
for them are in `contracts/walk.py`.

Outcome kinds follow Roadrunner's health check, because they drive what happens
to the stored artifact:

    ok            logged in; the prefix is good
    defect        a recorded step broke (portal drift): degrade the artifact
    auth          steps ran, still on the login surface: credentials, keep the artifact
    mfa_timeout   the code never cleared: inbox or mailbox, keep the artifact
    browser       the tab or navigation failed: infrastructure, no verdict
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.agents.browser import login_actions as la
from trailblazer.agents.browser.mfa import OtpWait, is_pre_portal_url
from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.contracts import LOGIN_OTP, WalkStep
from trailblazer.shared.carrier_creds import CarrierCreds

log = logging.getLogger(__name__)

Kind = Literal["ok", "defect", "auth", "mfa_timeout", "browser"]

# Sign-in URL fragments the MFA marker list does not cover (it is tuned to code screens).
EXTRA_LOGIN_MARKERS = ("/sign-in", "/signin", "/sign_in", "/log-in", "/session/new")


@dataclass
class LoginReplay:
    """What replaying a login prefix produced."""

    kind: Kind
    reason: str
    final_url: str = ""
    step_index: int | None = None
    step: WalkStep | None = None
    otp: OtpWait | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    @property
    def degrades_artifact(self) -> bool:
        """Only drift degrades the stored prefix; credentials and mailboxes are not its fault."""
        return self.kind == "defect"


def credential_locators(steps: list[WalkStep]) -> list[str]:
    """The inputs the prefix fills with credentials: their absence is what proves login."""
    return [s.locator for s in steps if s.action == "type" and s.credentialKey and s.credentialKey != LOGIN_OTP]


def on_login_surface(page: Page, steps: list[WalkStep], markers: list[str] | None = None) -> str | None:
    """Why the page still looks like a login screen, or None when it does not.

    A URL blocklist alone lies (a `/sign-in` login matches no marker; an OIDC
    callback matches one), so the prefix's own credential inputs are the
    authoritative signal, with the URL as a second opinion.
    """
    url = page.url
    if is_pre_portal_url(url, markers):
        return "url"
    if any(m in url.lower() for m in EXTRA_LOGIN_MARKERS):
        return "url"
    for sel in credential_locators(steps):
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return f"credential input {sel!r} still visible"
        except PlaywrightError:
            continue
    return None


def verify_logged_in(
    page: Page, steps: list[WalkStep], *, markers: list[str] | None = None, settle_s: float = 15.0
) -> tuple[bool, str]:
    """Did we actually get in? Settle, then re-check for a bounce back to login.

    OAuth flows land on a callback while the SPA boots, and a rejected session
    bounces back afterwards, so one look is not enough.
    """
    why = on_login_surface(page, steps, markers)
    waited = 0.0
    while why and waited < settle_s:
        page.wait_for_timeout(1000)
        waited += 1.0
        why = on_login_surface(page, steps, markers)
    if why:
        return False, f"still on the login screen ({why})"
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PlaywrightError:
        pass
    bounced = on_login_surface(page, steps, markers)
    if bounced:
        return False, f"bounced back to login once loaded ({bounced})"
    return True, "past the login surface"


def replay_login(
    page: Page,
    steps: list[WalkStep],
    *,
    credentials: CarrierCreds | None,
    inbox: OtpInbox | None = None,
    login_url: str | None = None,
    human_entry_possible: bool = False,
    mfa_timeout_s: float = 600.0,
    poll_s: float = 2.0,
    otp_settle_s: float = 12.0,
    markers: list[str] | None = None,
    step_timeout_ms: int = 15000,
    verify_settle_s: float = 15.0,
    settings: Any = None,
    on_handoff: Callable[[str], None] | None = None,
) -> LoginReplay:
    """Run the prefix step by step, then verify the tab is past the login surface.

    Each step resolves its locator to exactly one element (narrowing hidden
    look-alikes) and acts. A target that is missing gets one more look after any
    consent banner is answered, and a click an overlay intercepts is retried
    after the same; a recorded dismissal click is therefore replayed as
    recorded, not pre-empted. A click waits for the NEXT step's target to
    appear, so a hosted identity provider's redirect has time to land. A code
    step first confirms a code input is on screen: waiting on the inbox while
    the page still shows the password field would call a rejected login a
    missing code. Never raises: every failure is an outcome.
    """
    if not steps:
        return LoginReplay("defect", "the login prefix has no steps", final_url=page.url)
    try:
        if login_url:
            page.goto(login_url, wait_until="domcontentloaded")
    except PlaywrightError as e:
        return LoginReplay("browser", f"could not open {login_url}: {e}", final_url=page.url)

    anchor: str | None = None
    for i, step in enumerate(steps):
        label = f"step {i + 1} ({step.action} {step.locator})"
        try:
            if step.action == "type":
                if step.credentialKey == LOGIN_OTP:
                    if not _present(page, step.locator, step_timeout_ms):
                        return _defect_or_auth(page, steps, i, f"{label}: code input not found")
                    out = la.clear_otp(
                        page,
                        step.locator,
                        credentials,
                        inbox,
                        human_entry_possible=human_entry_possible,
                        timeout_s=mfa_timeout_s,
                        poll_s=poll_s,
                        settle_s=otp_settle_s,
                        markers=markers,
                        settings=settings,
                        on_handoff=on_handoff,
                    )
                    if not out.cleared:
                        return LoginReplay(
                            la.otp_error_class(out), f"{label}: {out.reason}", page.url, i, step, out
                        )
                    continue
                if step.credentialKey:
                    filled = la.fill_credential(page, step.locator, step.credentialKey, credentials, timeout_ms=step_timeout_ms)
                    if not filled.ok and filled.error == "not_found" and la.dismiss_overlays(page, budget_s=1.0):
                        filled = la.fill_credential(page, step.locator, step.credentialKey, credentials, timeout_ms=step_timeout_ms)
                    if not filled.ok:
                        if filled.error == "auth":
                            return LoginReplay("auth", f"{label}: no {step.credentialKey} on file", page.url, i, step)
                        return _defect_or_auth(page, steps, i, f"{label}: {filled.error}")
                    anchor = filled.selector
                    continue
                found = _find(page, step.locator, None, step_timeout_ms)
                if found.error:
                    return _defect_or_auth(page, steps, i, f"{label}: {found.error}")
                found.locator.fill(step.value or "", timeout=step_timeout_ms)
                continue

            if step.action in ("choose", "toggle"):
                found = _find(page, step.locator, None, step_timeout_ms)
                if found.error:
                    return _defect_or_auth(page, steps, i, f"{label}: {found.error}")
                if (found.locator.get_attribute("type") or "").lower() in ("radio", "checkbox"):
                    found.locator.check(timeout=step_timeout_ms)
                else:
                    found.locator.click(timeout=step_timeout_ms)
                continue

            if step.action in ("click", "back"):
                found = _find(page, step.locator, anchor, step_timeout_ms)
                if found.error:
                    return _defect_or_auth(page, steps, i, f"{label}: {found.error}")
                if not la.ensure_enabled(page, found.locator):
                    return LoginReplay("defect", f"{label}: control stayed disabled", page.url, i, step)
                before = page.url
                try:
                    found.locator.click(timeout=step_timeout_ms)
                except PlaywrightError:
                    # An overlay intercepting the click: answer it, then click once more.
                    la.dismiss_overlays(page, budget_s=1.0)
                    found.locator.click(timeout=step_timeout_ms)
                _await_next(page, steps, i, before, found.selector, step_timeout_ms)
                continue

            if step.action == "wait-for":
                try:
                    page.locator(step.locator).first.wait_for(state="visible", timeout=step_timeout_ms)
                except PlaywrightError:
                    return _defect_or_auth(page, steps, i, f"{label}: never appeared")
                continue

            return LoginReplay("defect", f"{label}: unknown action", page.url, i, step)

        except PlaywrightError as e:
            return _defect_or_auth(page, steps, i, f"{label}: {e.message.splitlines()[0] if hasattr(e, 'message') else e}")

    ok, why = verify_logged_in(page, steps, markers=markers, settle_s=verify_settle_s)
    if not ok:
        return LoginReplay("auth", why, page.url)
    log.info("login replayed: parked on %s", page.url)
    return LoginReplay("ok", why, page.url)


def _present(page: Page, selector: str, timeout_ms: int) -> bool:
    """Is the step's target on the page at all? Content decides, not the URL."""
    try:
        page.locator(selector).first.wait_for(state="attached", timeout=timeout_ms)
        return True
    except PlaywrightError:
        return False


def _find(page: Page, selector: str, anchor: str | None, timeout_ms: int) -> la.Resolved:
    """Resolve, and if the target is missing, answer any consent banner and look once more."""
    found = la.resolve_unique(page, selector, credential_anchor=anchor, timeout_ms=timeout_ms)
    if found.error == "not_found" and la.dismiss_overlays(page, budget_s=1.0):
        found = la.resolve_unique(page, selector, credential_anchor=anchor, timeout_ms=timeout_ms)
    return found


def _await_next(page: Page, steps: list[WalkStep], i: int, before: str, clicked: str, timeout_ms: int) -> None:
    """After a click, give the next step's target time to appear; the redirect may cross hosts."""
    nxt = steps[i + 1] if i + 1 < len(steps) else None
    if nxt is not None:
        try:
            page.locator(nxt.locator).first.wait_for(state="attached", timeout=timeout_ms)
            return
        except PlaywrightError:
            pass  # the next step's own resolve will name the failure
    la.settle_after_click(page, before, clicked)


def _defect_or_auth(page: Page, steps: list[WalkStep], i: int, reason: str) -> LoginReplay:
    """A missing target right after a sign-in click is usually a rejected login, not drift.

    The password was refused, the page stayed, and the code input the prefix
    expects next is not there. Content says which: if earlier credential inputs
    are still on screen, that is `auth`; otherwise the recipe broke.
    """
    step = steps[i]
    prev_was_click = i > 0 and steps[i - 1].action == "click"
    if prev_was_click and on_login_surface(page, steps[:i]) is not None:
        return LoginReplay("auth", f"sign-in did not move on; {reason}", page.url, i, step)
    return LoginReplay("defect", reason, page.url, i, step)
