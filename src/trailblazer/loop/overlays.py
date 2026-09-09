"""Clearing dialogs that sit over the form, outside Frontier.

A dialog is not a question. Left to Frontier it became one: a "Close" was a
route step, remembered as pressed, refused on the next walk, and every fill
under the backdrop failed (run 8fdb484afefe: 4 restarts, 12 looks, 20 blocked
fills on one popup). Here a dialog is cleared before the page is described, and
how it was cleared is written down once -- its heading and the button that
worked -- so a restart's re-execution and the replay script clear the same
dialog from the record, with no model call.

Two paths meet in the record. A dialog already in the table is cleared by
`clear_known` before any look, on the DOM alone. One not yet in it reaches the
scraper inside the look that was going to happen anyway; the model names the
clickable that closes it and keeps the answers, and `dismiss` presses that
measured address and adds the entry.
"""

import logging
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.agents.browser.write_tools import RefusedError, wait_settled
from trailblazer.agents.form_filler.safety import refuse_if_denied
from trailblazer.contracts.page_description import Overlay
from trailblazer.observability.events import event
from trailblazer.observability.logging import get_logger

log = get_logger(__name__)

# The same fingerprint rule as extract.js: heading, else first line.
_DIALOGS_JS = """
() => {
  const vis = (n) => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  return [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], [aria-modal="true"]')]
    .filter(vis)
    .map((d) => {
      const h = d.querySelector('h1, h2, h3, h4, h5, [role="heading"]');
      const lines = (d.innerText || '').split('\\n').map((s) => s.trim()).filter(Boolean);
      return {
        title: ((h && h.innerText.trim()) || lines[0] || '').slice(0, 80),
        hasControls: [...d.querySelectorAll('input, select, textarea')].some((e) => e.type !== 'hidden' && vis(e)),
      };
    });
}
"""

_GONE_TIMEOUT_MS = 5_000
_MAX_ROUNDS = 3


class OverlayTable:
    """title -> the measured locator that cleared it. One crawl's memory of dialogs."""

    def __init__(self) -> None:
        self.dismissals: dict[str, str] = {}

    def get(self, title: str) -> str | None:
        return self.dismissals.get(title)

    def add(self, title: str, locator: str) -> bool:
        """Record one dismissal. False if the title was already known."""
        if title in self.dismissals:
            return False
        self.dismissals[title] = locator
        return True


def visible_notices(page: Page) -> list[dict]:
    """Dialogs showing now that hold no fillable control, by fingerprint."""
    try:
        return [d for d in page.evaluate(_DIALOGS_JS) if not d["hasControls"]]
    except PlaywrightError:
        return []  # mid-navigation the document can be replaced under the call


def clear_known(page: Page, table: OverlayTable) -> list[str]:
    """Clear every showing dialog the table already knows. Returns their titles.

    No model. A dialog not in the table is left for the look that follows,
    which is where the model first names its dismisser. Bounded so a dialog
    that survives its recorded dismissal stops here rather than looping.
    """
    cleared: list[str] = []
    for _ in range(_MAX_ROUNDS):
        pending = visible_notices(page)
        if not pending:
            break
        title = pending[0]["title"]
        locator = table.get(title)
        if locator is None:
            event("dismiss", "loop", dialog=title, action="left for the scraper")
            break
        event("dismiss", "loop", dialog=title, locator=locator, via="recorded")
        gone = _press(page, locator, title)
        cleared.append(title)
        if not gone:
            break  # pressing again would loop; the look that follows reports it
    return cleared


def dismiss(page: Page, overlay: Overlay) -> str:
    """Press the clickable the scraper named for this notice. Returns its locator."""
    chosen = next((c for c in overlay.clickables if c.key == overlay.dismissKey), None)
    if chosen is None or not chosen.locator:
        raise RuntimeError(f"dialog {overlay.title!r}: dismissKey {overlay.dismissKey!r} names no measured clickable")
    event("dismiss", "loop", dialog=overlay.title, via=chosen.label, locator=chosen.locator)
    _press(page, chosen.locator, overlay.title)
    return chosen.locator


def _press(page: Page, locator: str, title: str) -> bool:
    """Click, denylist first, then wait for that dialog to go. True if it went."""
    element = page.locator(locator).first
    reason = refuse_if_denied(element, locator)
    if reason is not None:
        raise RefusedError(reason)
    element.click(timeout=_GONE_TIMEOUT_MS)
    deadline = time.monotonic() + _GONE_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        if all(d["title"] != title for d in visible_notices(page)):
            wait_settled(page)
            return True
        page.wait_for_timeout(200)
    event("dismiss", "loop", logging.WARNING, dialog=title, locator=locator,
          detail=f"still showing after {_GONE_TIMEOUT_MS}ms")
    return False
