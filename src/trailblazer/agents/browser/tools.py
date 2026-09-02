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
