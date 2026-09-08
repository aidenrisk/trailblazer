"""Browser tools that change the page. The form filler's tools, and only its.

Separate from `tools.py` because that module's docstring declares itself
read-only and names that as the structural reason the scraper cannot act: "there
is no clicking tool to call". Adding `click` beside `read_snapshot` would make
the claim false the moment someone passed the whole list to the wrong agent.
Two modules keep the separation a fact about imports rather than a convention.

Every function takes a locator the scraper measured and Frontier put in an
Assignment. Nothing here builds or repairs a selector: a locator that does not
resolve to exactly one node is an error returned to the caller, which becomes a
blocked FillReport, not a cue to improvise a different address.

Every click path -- `click` and `select_option`'s option-locator branch --
passes through `safety.refuse_if_denied` first. That check is in this module's
dispatch path rather than in the caller so that no future caller can reach a
click without it.
"""

import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.agents.form_filler.safety import refuse_if_denied
from trailblazer.observability.logging import get_logger

log = get_logger(__name__)

_ACTION_TIMEOUT_MS = 5_000
"""Per-action cap. Short: a control the assignment names should already exist,
and a locator that needs thirty seconds to resolve is a broken locator."""

_SETTLE_TIMEOUT_MS = 5_000
"""How long `advance` waits for the network to go quiet after a navigating click."""


class LocatorError(RuntimeError):
    """A locator did not resolve to exactly one element on the live page.

    Raised rather than repaired: the filler's contract is that locators are
    measured elsewhere, so the only correct response is a blocked report.
    """


class RefusedError(RuntimeError):
    """A click was refused by the no-pay/no-bind denylist. Never retried."""


def resolve(page: Page, locator: str):
    """Return the single element `locator` addresses, or raise `LocatorError`.

    Uniqueness is checked, not assumed. A selector matching two nodes would have
    Playwright act on the first, which silently fills the wrong field and
    records a value against a `fieldId` that never received it.
    """
    try:
        found = page.locator(locator)
        count = found.count()
    except PlaywrightError as e:
        raise LocatorError(f"locator {locator!r} is not a usable selector: {e}") from e
    if count == 0:
        raise LocatorError(f"locator {locator!r} matched no element on {page.url}")
    if count > 1:
        raise LocatorError(f"locator {locator!r} matched {count} elements; it is not unique")
    return found


_EQUIVALENCE_JS = """
(el) => [
  el.tagName.toLowerCase(),
  el.getAttribute('href') || '',
  (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\\s+/g, ' '),
].join('\\x1f')
"""
"""What makes two matches the same destination: tag, href and accessible name."""


def resolve_click_target(page: Page, locator: str):
    """The element to click, allowing one visible node among equivalent duplicates.

    `resolve` demands exactly one match, which is right for a fill: two inputs
    sharing a selector are different fields, and typing into whichever came
    first records a value against a control that never received it -- invisibly,
    since the wrong locator still resolves to one node.

    A duplicated nav link is not that. A responsive layout renders the same link
    twice and hides the copy that does not apply, so both matches carry the same
    tag, the same href and the same name, and clicking either does the same
    thing. Pie renders its whole nav that way, and "Get a Quote" was unclickable
    because of it.

    So the relaxation is bounded twice over: it is only reachable from `click`,
    never from `fill`, and it applies only when every match is equivalent and
    exactly one of them is visible. Two visible matches, or matches that differ,
    raise as before -- that is a real ambiguity and guessing at it is what the
    uniqueness rule exists to prevent.
    """
    try:
        found = page.locator(locator)
        count = found.count()
    except PlaywrightError as e:
        raise LocatorError(f"locator {locator!r} is not a usable selector: {e}") from e
    if count == 0:
        raise LocatorError(f"locator {locator!r} matched no element on {page.url}")
    if count == 1:
        return found

    try:
        signatures = {found.nth(i).evaluate(_EQUIVALENCE_JS) for i in range(count)}
        visible = [i for i in range(count) if found.nth(i).is_visible()]
    except PlaywrightError as e:
        raise LocatorError(f"locator {locator!r} matched {count} elements: {e}") from e

    if len(signatures) > 1:
        raise LocatorError(
            f"locator {locator!r} matched {count} elements that are not the same "
            "control; it is not unique"
        )
    if len(visible) != 1:
        # A dialog often offers the same dismissal twice: an icon-only x and a
        # labelled "Close". Both are visible and both compute the same name, so
        # they are equivalent -- but only one carries its own text, and that is
        # the button a person would press. Pie's bureau notice stayed open on
        # this tie, and the modal hid the page's buttons from the accessibility
        # tree for the rest of the walk.
        try:
            with_text = [
                i for i in visible
                if (found.nth(i).evaluate("n => (n.textContent || '').trim()") or "")
            ]
        except PlaywrightError:
            with_text = []
        if len(with_text) == 1:
            log.info("locator %r: %d visible copies, one labelled; clicking the labelled one", locator, len(visible))
            return found.nth(with_text[0])
        raise LocatorError(
            f"locator {locator!r} matched {count} equivalent elements with "
            f"{len(visible)} visible; exactly one must be"
        )
    log.info(
        "locator %r matched %d equivalent copies; clicking the visible one",
        locator,
        count,
    )
    return found.nth(visible[0])


def fill(page: Page, locator: str, value: str) -> None:
    """Type `value` into the control at `locator`, replacing what is there.

    `Locator.fill` is used rather than `type`: it clears first and dispatches the
    input and change events frameworks listen for, which a keystroke simulation
    on a React-controlled input does not reliably do.

    The field is blurred afterwards. A form commits and validates a value when
    focus leaves it, and `fill` on its own never moves focus: on Pie every
    field displayed its value, nothing was ever validated, Next did nothing and
    reported nothing, and the crawl called the page finished. A person's typing
    always ends with focus leaving the field; so does this.
    """
    element = resolve(page, locator)
    try:
        element.fill(value, timeout=_ACTION_TIMEOUT_MS)
        element.blur(timeout=_ACTION_TIMEOUT_MS)
    except PlaywrightError as e:
        raise LocatorError(f"could not type into {locator!r}: {e}") from e


def type_text(page: Page, locator: str, value: str) -> None:
    """Type `value` and leave focus in the field.

    For a typeahead: the suggestions it raises exist only while the field is
    focused, so the blur that `fill` performs would dismiss them before one
    could be picked. The pick is what commits the value.
    """
    element = resolve(page, locator)
    try:
        element.fill(value, timeout=_ACTION_TIMEOUT_MS)
    except PlaywrightError as e:
        raise LocatorError(f"could not type into {locator!r}: {e}") from e


def click(page: Page, locator: str) -> None:
    """Click the element at `locator`, after the denylist check.

    The check runs on the resolved element and before dispatch. A refusal raises
    `RefusedError`, which the filler turns into a blocked report; it never falls
    through to the click.

    Resolution is `resolve_click_target`, not `resolve`: a nav link rendered
    twice by a responsive layout is one destination, and the denylist still runs
    on whichever copy is visible.
    """
    element = resolve_click_target(page, locator)
    reason = refuse_if_denied(element, locator)
    if reason is not None:
        raise RefusedError(reason)
    try:
        element.click(timeout=_ACTION_TIMEOUT_MS)
    except PlaywrightError as e:
        raise LocatorError(f"could not click {locator!r}: {e}") from e


def select_option(page: Page, locator: str, label: str) -> str:
    """Set the native `<select>` at `locator` to the option carrying `label`.

    Returns the label actually selected. Matching is by visible label first and
    by value second, because a `PageDescription` records the option's label and
    the underlying value is often an opaque code (`llc`, `corp`).
    """
    element = resolve(page, locator)
    try:
        element.select_option(label=label, timeout=_ACTION_TIMEOUT_MS)
        return label
    except PlaywrightError:
        pass
    try:
        element.select_option(value=label, timeout=_ACTION_TIMEOUT_MS)
        return label
    except PlaywrightError as e:
        raise LocatorError(
            f"{locator!r} has no option with label or value {label!r}: {e}"
        ) from e


def read_page_text(page: Page) -> str:
    """The page's visible text.

    Read after a rejected fill, where the format requirement a field enforces
    exists nowhere else -- not in the PageDescription, whose `Control` has no
    format field, and not in the Assignment.
    """
    try:
        return page.locator("body").inner_text()
    except PlaywrightError as e:
        raise LocatorError(f"could not read the page text: {e}") from e


_TRANSITION_TIMEOUT_MS = 45_000
"""How long a forward press is given to move the page.

Pie's Next validates client-side first and navigates 4.6 seconds later on one
branch and 7.5 on another, measured; network-idle is satisfied long before
either, so idle alone read the old page and called the press a no-op. An
8-second budget then called the 7.5-second branch stuck and ended a run; a
20-second budget did the same on a run where the same page and values moved in
11.1 seconds when probed. Dialog dismissals no longer pass through here, so the
budget is spent only on a press that was genuinely refused.
"""


def wait_for_transition(page: Page, from_url: str, timeout_ms: int = _TRANSITION_TIMEOUT_MS) -> bool:
    """Wait for the page to leave `from_url`, then settle. True if it moved.

    A forward control on a single-page app often does its work in two phases:
    validate in the browser, then navigate. Network-idle can be satisfied between
    the two, so it is not evidence that the press did nothing. The URL is polled
    until it changes or the budget runs out; either way the network is then
    given its chance to go quiet, so the look that follows sees a settled page.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    moved = False
    while time.monotonic() < deadline:
        if page.url != from_url:
            moved = True
            break
        page.wait_for_timeout(250)
    wait_settled(page)
    return moved


_CONTENT_TIMEOUT_MS = 20_000
_HAS_CONTENT_JS = """
() => {
  const vis = (n) => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const controls = [...document.querySelectorAll('input, select, textarea')].filter((e) => e.type !== 'hidden' && vis(e)).length;
  const dialog = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], [aria-modal="true"]')].some(vis);
  return controls > 0 || dialog;
}
"""


def wait_for_content(page: Page, timeout_ms: int = _CONTENT_TIMEOUT_MS) -> bool:
    """After a page change, wait until it has something to act on. True if it does.

    A page that has just been reached may still be assembling itself: Pie's
    workforce page runs a bureau lookup on arrival and shows a spinner with no
    fields for two seconds, then a dialog over the form. A look taken at the
    instant the URL changed saw zero controls, spent a model call describing
    the spinner, and left Frontier with nothing to do but click a backwards
    step tab. Polled cheaply on the DOM, no model, until a settable control or
    a dialog is visible.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            if page.evaluate(_HAS_CONTENT_JS):
                return True
        except PlaywrightError:
            pass  # mid-navigation the document can be replaced under the call
        page.wait_for_timeout(250)
    log.warning("page showed no controls and no dialog within %dms: %s", timeout_ms, page.url)
    return False


def wait_settled(page: Page, timeout_ms: int = _SETTLE_TIMEOUT_MS) -> None:
    """Wait for the network to go quiet, tolerating a page that never does.

    An SPA holding a websocket or a poller never reaches networkidle, and that
    is not a failure of the click that preceded it -- the page moved on either
    way. The timeout is swallowed here and nowhere else in this module.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightError:
        log.debug("page did not reach networkidle within %dms; continuing", timeout_ms)
