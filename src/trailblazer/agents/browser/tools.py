"""Read-only browser tools, shared by agents that look but do not act.

The scraper is registered with exactly these four. No click, type, fill, or
select tool is built here, so "the scraper looks, it does not act" is enforced
structurally rather than by a line in the system prompt: there is no clicking
tool to call. Write tools for the form filler belong in this module too, but
the scraper simply never passes them to its agent.
"""

from langchain_core.tools import BaseTool, tool
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page


def read_only_tools(page: Page) -> list[BaseTool]:
    """Build the four read-only tools as closures over a live `Page`."""

    @tool
    def read_snapshot() -> str:
        """Return the page's accessibility tree as text (roles and names)."""
        try:
            return page.locator("body").aria_snapshot()
        except PlaywrightError as e:
            return f"error: {e}"

    @tool
    def count_matches(selector: str) -> str:
        """Count the elements a Playwright selector matches. Use to check a locator."""
        try:
            return str(page.locator(selector).count())
        except PlaywrightError as e:
            return f"error: {e}"

    @tool
    def current_url() -> str:
        """Return the URL of the page currently loaded."""
        return page.url

    @tool
    def wait_then_resnapshot(ms: int) -> str:
        """Wait `ms` milliseconds, then return a fresh snapshot. For pages that settle late."""
        page.wait_for_timeout(min(ms, 10_000))
        try:
            return page.locator("body").aria_snapshot()
        except PlaywrightError as e:
            return f"error: {e}"

    return [read_snapshot, count_matches, current_url, wait_then_resnapshot]


def write_tools(page: Page) -> list[BaseTool]:
    """The acting half, reserved above and built here for the form filler.

    Handed out ONLY to a recovery attempt, and only after a deterministic
    execution has already failed. The filler's normal path never sees these:
    an Assignment carries an exact locator, so "fill this" is a call, not a
    decision, and a model given write tools on the happy path would quietly
    route around locator bugs that ought to surface.

    Every tool returns a string because that is what a tool-calling model
    consumes; failures come back as `error: ...` rather than raising, so one
    bad selector costs the model a turn instead of ending the walk.
    """

    @tool
    def fill_field(selector: str, value: str) -> str:
        """Type `value` into the element `selector` matches. Clears it first."""
        try:
            page.locator(selector).fill(value)
            return f"filled {selector}"
        except PlaywrightError as e:
            return f"error: {e}"

    @tool
    def click(selector: str) -> str:
        """Click the element `selector` matches."""
        try:
            page.locator(selector).click()
            return f"clicked {selector}"
        except PlaywrightError as e:
            return f"error: {e}"

    @tool
    def select_option(selector: str, label: str) -> str:
        """Choose the option labelled `label` on the native <select> `selector` matches."""
        try:
            page.locator(selector).select_option(label=label)
            return f"selected {label!r} on {selector}"
        except PlaywrightError as e:
            return f"error: {e}"

    @tool
    def check(selector: str) -> str:
        """Tick the checkbox or radio `selector` matches."""
        try:
            page.locator(selector).check()
            return f"checked {selector}"
        except PlaywrightError as e:
            return f"error: {e}"

    @tool
    def read_value(selector: str) -> str:
        """Return what the input `selector` matches currently holds. Use to verify."""
        try:
            return page.locator(selector).input_value()
        except PlaywrightError as e:
            return f"error: {e}"

    return [fill_field, click, select_option, check, read_value]
