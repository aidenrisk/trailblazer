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
