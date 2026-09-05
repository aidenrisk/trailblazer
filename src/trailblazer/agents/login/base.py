"""The login journey, held once and specialised per carrier.

Every portal asks the same four things in the same order -- reach a sign-in
page, put a username and a password into it, submit, and prove the session took
-- and differs only in what those steps address. So the order lives here as a
`final` template and a carrier overrides the parts that differ, which is what
makes one class serve every NAICS code and every state for that carrier.

Why a class rather than a discovered walk. The crawl can perceive a login page
like any other, but it cannot grade the result: a portal that answers a bad
password with a re-rendered form looks, to a perceiver, like a page that simply
did not change. `authenticated()` is the carrier's own assertion about its own
DOM, and without it a failed login is walked as if it were the application form.

Credentials never travel. `CarrierCreds` is read at the moment of typing and the
literal is not stored on the instance, not logged, and not returned in
`LoginResult`; the metadata artifact keeps `$EMAIL`/`$PASSWORD` placeholders.

MFA is not solved here. `awaiting_mfa()` reports that a portal is asking for a
code and `LoginResult.mfa_required` says so; supplying it is out of scope,
because a one-time code is by definition not in the credential store.
"""

import time
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.observability.logging import get_logger
from trailblazer.shared.dev_carrier_creds import CarrierCreds

log = get_logger(__name__)

_SETTLE_MS = 10_000
"""How long a submit is given to land before the result is graded."""

_PROBE_MS = 2_000
"""How long a question about the page's current state waits. Short: it asks what
is true now, unlike the grade after a submit, which waits for the page to arrive."""


class LoginError(RuntimeError):
    """The session could not be authenticated. Never swallowed, never defaulted."""


@dataclass
class LoginStep:
    """One action the login performed, in the form the replay script re-runs.

    `value` is the placeholder, never the literal: `$EMAIL` and `$PASSWORD` are
    what reach the metadata artifact, and the replay script resolves them from
    its own config file at run time.
    """

    action: str
    """`goto`, `fill`, or `click`."""

    selector: str = ""
    value: str = ""


@dataclass
class LoginResult:
    """What happened, in terms the caller can route on.

    `ok` is `authenticated()` measured after the submit, not the absence of an
    exception: a portal that re-renders its form on a bad password raises
    nothing at all.
    """

    ok: bool
    url: str
    mfa_required: bool = False
    reason: str = ""
    steps: list[LoginStep] = field(default_factory=list)
    """What was performed, in order, so the replay script can re-run it.

    Recorded rather than re-derived: the crawl is the only thing that has stood
    on the sign-in page, and a script that cannot log itself in starts every
    replay on a page it is not authenticated for.
    """


class CarrierLogin:
    """One carrier's sign-in journey.

    Subclass and override the selectors, or a whole step where the portal does
    something the default cannot express. `sign_in` itself is the fixed order
    and is not overridden -- a carrier that needs a different order needs a
    different step, not a different journey.
    """

    carrier_id: str = ""
    """The key `resolve_login` matches on."""

    username_selector: str = "input[type=email], input[name=email], input[name=username]"
    password_selector: str = "input[type=password]"
    submit_selector: str = "button[type=submit]"

    authenticated_selector: str = ""
    """A node that exists only once signed in. Required: see `authenticated`."""

    mfa_selector: str = ""
    """A node that appears when the portal asks for a one-time code."""

    def __init__(self, creds: CarrierCreds) -> None:
        self.creds = creds
        self.steps: list[LoginStep] = []
        """What has been performed, appended by the steps as they run.

        Recorded by the step that acts rather than assembled afterwards, so a
        carrier that overrides a step -- or inserts one, as Pie does to clear
        its privacy dialog -- records what it actually did.
        """

    def record(self, action: str, selector: str = "", value: str = "") -> None:
        """Append one performed action. `value` must be a placeholder, not a literal."""
        self.steps.append(LoginStep(action=action, selector=selector, value=value))

    def recorded_journey(self) -> list[LoginStep]:
        """The login the replay script must perform, whether or not it ran here.

        A crawl whose profile still holds a session skips the sign-in, but the
        replay runs in a fresh browser and cannot skip it. Declared from the
        class's own selectors rather than from what was performed, so the script
        gets a login block either way.
        """
        return [
            LoginStep("goto", value=self.creds.login_url),
            LoginStep("fill", selector=self.username_selector, value="$EMAIL"),
            LoginStep("fill", selector=self.password_selector, value="$PASSWORD"),
            LoginStep("click", selector=self.submit_selector),
        ]

    # -- the journey, in order ---------------------------------------------

    def sign_in(self, page: Page) -> LoginResult:
        """Run the whole journey and grade it. The one entry point.

        Grading is separate from doing: `authenticated()` is asked after the
        submit settles, so a portal that answers a rejected password with the
        same form reports `ok=False` rather than passing silently.
        """
        started = time.monotonic()
        log.info("login start carrier_id=%s url=%s", self.carrier_id, self.creds.login_url)
        self.steps = []

        if self.already_signed_in(page):
            log.info("login skipped carrier_id=%s: session already authenticated",
                     self.carrier_id)
            # No steps: the replay script must still be able to log in, so a
            # skipped login records the journey it did not need to perform.
            return LoginResult(
                ok=True, url=page.url, reason="already signed in",
                steps=self.recorded_journey(),
            )

        self.navigate(page)
        self.enter_username(page)
        self.enter_password(page)
        self.submit(page)
        self.settle(page)

        if self.awaiting_mfa(page):
            # Not an error: the credentials were accepted and a human can finish
            # in the shared headed browser. Reported so the caller can wait.
            log.warning("login needs MFA carrier_id=%s url=%s", self.carrier_id, page.url)
            return LoginResult(
                ok=False, url=page.url, mfa_required=True,
                reason="the portal is asking for a one-time code",
                steps=list(self.steps),
            )

        ok = self.authenticated(page)
        ms = int((time.monotonic() - started) * 1000)
        log.info("login end carrier_id=%s ok=%s url=%s ms=%d",
                 self.carrier_id, ok, page.url, ms)
        if not ok:
            return LoginResult(
                ok=False, url=page.url,
                reason=f"{self.authenticated_selector!r} not present after submit",
                steps=list(self.steps),
            )
        return LoginResult(ok=True, url=page.url, steps=list(self.steps))

    def wait_for_manual_completion(self, page: Page, timeout_s: int) -> LoginResult:
        """Poll until the session is authenticated, or give up.

        For a headed run stopped at a one-time code: the credentials were
        accepted, the code is not in the credential store, and a human is
        watching the window. Waiting is cheaper than failing.

        Polls `authenticated()` rather than watching the MFA field disappear: a
        portal may clear the prompt and still not have signed the session in,
        and it is the signed-in marker that the crawl depends on.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.authenticated(page):
                log.info("login completed by hand carrier_id=%s", self.carrier_id)
                return LoginResult(ok=True, url=page.url, steps=self.recorded_journey())
            page.wait_for_timeout(2_000)

        return LoginResult(
            ok=False,
            url=page.url,
            mfa_required=True,
            reason=f"no one-time code entered within {timeout_s}s",
            steps=self.recorded_journey(),
        )

    # -- steps a carrier may override --------------------------------------

    def navigate(self, page: Page) -> None:
        """Open the sign-in page."""
        page.goto(self.creds.login_url, wait_until="domcontentloaded")
        self.record("goto", value=self.creds.login_url)

    def enter_username(self, page: Page) -> None:
        """Type the username. The literal is read here and not retained."""
        if not self.creds.username:
            raise LoginError(f"no username configured for carrier {self.carrier_id!r}")
        self._fill(page, self.username_selector, self.creds.username, "username", "$EMAIL")

    def enter_password(self, page: Page) -> None:
        """Type the password. The literal is read here and not retained."""
        if not self.creds.password:
            raise LoginError(f"no password configured for carrier {self.carrier_id!r}")
        self._fill(page, self.password_selector, self.creds.password, "password", "$PASSWORD")

    def submit(self, page: Page) -> None:
        """Press the sign-in control.

        Not routed through the no-pay denylist: this is the one click the crawl
        makes before an application exists, on a page with no money on it. The
        denylist guards the application walk, where a submit may be a bind.

        A detached element after the click is success, not failure. Pie's button
        is `disabled` until the form validates, so Playwright retries; the click
        that lands navigates, the button goes with the old document, and the
        retry then raises against an element that is gone. Whether the sign-in
        took is `authenticated()`'s answer, not this click's.
        """
        element = self._one(page, self.submit_selector, "submit control")
        try:
            element.click(timeout=_SETTLE_MS)
        except PlaywrightError as e:
            if "detached" not in str(e) and "not attached" not in str(e):
                raise
            log.debug("submit control detached after the click; the page navigated")
        self.record("click", selector=self.submit_selector)

    def settle(self, page: Page) -> None:
        """Wait for the submit to land, tolerating a portal that polls forever."""
        try:
            page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
        except PlaywrightError:
            log.debug("login: network did not go quiet within %dms", _SETTLE_MS)

    def already_signed_in(self, page: Page) -> bool:
        """True when the profile still holds a session from an earlier run.

        Checked before navigating, because the persistent Chromium profile keeps
        cookies between runs and re-submitting a login that is already good
        wastes a round trip and can log the session out.
        """
        return self._present(page, self.authenticated_selector)

    def authenticated(self, page: Page) -> bool:
        """The carrier's own assertion that the session took.

        Required rather than defaulted to `True`: a login that reports success
        it did not verify sends the crawl to perceive a sign-in page and record
        it as the carrier's application form.
        """
        if not self.authenticated_selector:
            raise LoginError(
                f"{type(self).__name__} sets no `authenticated_selector`, so a "
                "successful sign-in cannot be told from a rejected one"
            )
        # Waited for, not probed: `settle` returns when the network goes quiet,
        # which on an SPA is before the dashboard has finished mounting. Pie
        # signed in and landed on /search with the nav still not rendered, and a
        # probe read that as a rejected password.
        return self._present(page, self.authenticated_selector, _SETTLE_MS)

    def awaiting_mfa(self, page: Page) -> bool:
        """True when the portal is asking for a one-time code."""
        return bool(self.mfa_selector) and self._present(page, self.mfa_selector)

    # -- helpers ------------------------------------------------------------

    def _fill(
        self, page: Page, selector: str, value: str, what: str, placeholder: str = ""
    ) -> None:
        """Type into exactly one element. The literal is neither logged nor recorded."""
        element = self._one(page, selector, what)
        element.fill(value, timeout=_SETTLE_MS)
        log.info("login typed %s carrier_id=%s", what, self.carrier_id)
        if placeholder:
            self.record("fill", selector=selector, value=placeholder)

    def _one(self, page: Page, selector: str, what: str):
        """The single element `selector` addresses, or raise.

        An ambiguous login field is not guessed at with `.first`: typing a
        password into whichever box happened to be first is how a run reports a
        bad credential that was never actually sent.
        """
        located = page.locator(selector)
        try:
            count = located.count()
        except PlaywrightError as e:
            raise LoginError(f"{what} selector {selector!r} is not usable: {e}") from e
        if count != 1:
            raise LoginError(
                f"{what} selector {selector!r} matched {count} elements on "
                f"{page.url}; exactly one is required"
            )
        return located

    def _present(self, page: Page, selector: str, timeout_ms: int = _PROBE_MS) -> bool:
        """True when `selector` resolves to at least one visible node.

        Every match is checked, not `.first`. A responsive layout renders its
        nav twice and hides the copy that does not apply, so Pie's signed-in
        marker matched two nodes with the hidden one first: `.first.is_visible()`
        answered False on a page that was signed in, and the login was reported
        as a rejected password.

        Polled rather than waited on, because Playwright's own wait applies to
        one locator and the question here is about the set.
        """
        if not selector:
            return False
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                located = page.locator(selector)
                if any(located.nth(i).is_visible() for i in range(located.count())):
                    return True
            except PlaywrightError:
                pass  # mid-render the page can refuse the query; retry until the deadline
            if time.monotonic() >= deadline:
                return False
            page.wait_for_timeout(250)
