"""How the page is turned into a payload the model can judge.

Deterministic DOM extraction supplies identity and a *verified* locator;
the accessibility snapshot supplies role and accessible name. The model is
then asked only for judgment -- clean labels, type normalisation, blockers --
and never to invent a selector.

Two implementations behind one protocol, selected by `SCRAPER_PERCEIVER`,
because which one wins is a property of the portal, not of the design.
"""

import json
from pathlib import Path
from typing import Any, Protocol

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.observability.logging import get_logger

log = get_logger(__name__)

_EXTRACT_JS = (Path(__file__).parent / "extract.js").read_text()

# Buttons that move between pages. Read-only: found, never clicked.
_NEXT_PATTERNS = ["next", "continue", "save and continue", "submit", "get quote"]
_BACK_PATTERNS = ["back", "previous", "return"]


def _first_unique(page: Page, candidates: list[str]) -> tuple[str, bool]:
    """Return the first candidate locator matching exactly one node.

    Uniqueness is measured here rather than in the page because Playwright's
    selector engines do not exist in the DOM. If nothing is unique the first
    candidate is returned with `unique=False` -- reported honestly, never
    papered over with `.nth()`, because the form filler and replay gen both
    fail loudly on an ambiguous locator.
    """
    for sel in candidates:
        try:
            count = page.locator(sel).count()
            if count == 1:
                return sel, True
            log.debug("locator not unique: %r matched %d nodes", sel, count)
        except PlaywrightError as e:
            log.debug("locator rejected: %r (%s)", sel, e)
            continue  # malformed or unsupported selector; try the next
    return (candidates[0] if candidates else ""), False


def _prefer_visible_text(page: Page, loc: Any, fallback: str) -> str:
    """Rebuild the locator around the button's own text, if that stays unique.

    The pattern that found the button is a lowercase search term; the contract
    documents the literal text. Swapping one for the other can widen the match
    (the pattern "continue" finds a "Save and Continue" button), so the rebuilt
    locator is re-measured and kept only when it still resolves to one node.
    """
    try:
        text = (loc.inner_text() or "").strip()
        if not text or '"' in text:
            return fallback
        rebuilt = f'button:has-text("{text}")'
        return rebuilt if page.locator(rebuilt).count() == 1 else fallback
    except PlaywrightError:
        return fallback


def _find_button(page: Page, patterns: list[str]) -> str | None:
    """Locator for the first visible button whose text matches one of `patterns`.

    The emitted locator carries the button's *visible* text, not the lowercase
    search pattern that found it, so a "Next" button yields
    `button:has-text("Next")` as `scraper_io.txt` documents. `:has-text()` is
    case-insensitive either way; matching the documented literal is what makes
    the output comparable against the contract.
    """
    for word in patterns:
        sel = f'button:has-text("{word}")'
        try:
            loc = page.locator(sel)
            if loc.count() == 1 and loc.is_visible():
                return _prefer_visible_text(page, loc, sel)
        except PlaywrightError:
            continue
    return None


def _aria_snapshot(page: Page) -> str:
    """The browser's own answer to what a screen reader would announce.

    Playwright 1.62 removed `page.accessibility.snapshot()` (which returned a
    nested dict); `Locator.aria_snapshot()` is the replacement and yields YAML
    role/name lines. It resolves label-to-control association using the full
    rule set, which is the hardest part of reading a form and comes free.
    """
    try:
        return page.locator("body").aria_snapshot()
    except PlaywrightError:
        return ""


class Perceiver(Protocol):
    """One look at a page, rendered as the payload handed to the model."""

    def perceive(self, page: Page) -> dict[str, Any]:
        """Return `{url, title, controls, a11y, next, back}`."""
        ...


class DomSnapshotPerceiver:
    """DOM extraction for addressability, accessibility snapshot for semantics."""

    def perceive(self, page: Page) -> dict[str, Any]:
        """Extract controls, verify each locator, and attach the a11y tree."""
        raw: list[dict[str, Any]] = page.evaluate(_EXTRACT_JS)
        log.debug("extractor returned %d raw elements", len(raw))

        controls = []
        for item in raw:
            locator, unique = _first_unique(page, item.get("candidates", []))
            controls.append({**{k: v for k, v in item.items() if k != "candidates"},
                             "locator": locator, "unique": unique})

        return {
            "url": page.url,
            "title": page.title(),
            "controls": controls,
            "a11y": _aria_snapshot(page),
            "next": _find_button(page, _NEXT_PATTERNS),
            "back": _find_button(page, _BACK_PATTERNS),
        }


class A11yOnlyPerceiver:
    """Accessibility snapshot alone. No extraction, so no verified locators.

    Kept as the documented alternative for pages whose markup carries no usable
    identity attributes. The model must then propose locators itself, which is
    why this is not the default.
    """

    def perceive(self, page: Page) -> dict[str, Any]:
        """Return the flattened a11y tree with an empty controls list."""
        log.warning(
            "a11y perceiver in use: no locator is measured, so the model must propose "
            "selectors and `unique` cannot be verified"
        )
        return {
            "url": page.url,
            "title": page.title(),
            "controls": [],
            "a11y": _aria_snapshot(page),
            "next": _find_button(page, _NEXT_PATTERNS),
            "back": _find_button(page, _BACK_PATTERNS),
        }


def get_perceiver(kind: str) -> Perceiver:
    """Select the implementation named by `SCRAPER_PERCEIVER`."""
    return A11yOnlyPerceiver() if kind == "a11y" else DomSnapshotPerceiver()


def payload_to_text(payload: dict[str, Any]) -> str:
    """Render the payload as the human message body for the model."""
    return json.dumps(payload, indent=2)
