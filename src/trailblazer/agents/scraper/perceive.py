"""How the page is turned into a payload the model can judge.

Deterministic DOM extraction supplies identity and a *verified* locator;
the accessibility snapshot supplies role and accessible name. The model is
then asked only for judgment -- clean labels, type normalisation, blockers --
and never to invent a selector.

Two implementations behind one protocol, selected by `SCRAPER_PERCEIVER`,
because which one wins is a property of the portal, not of the design.
"""

import json
import re
from pathlib import Path
from typing import Any, Protocol

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.observability.logging import get_logger

log = get_logger(__name__)

_EXTRACT_JS = (Path(__file__).parent / "extract.js").read_text()

# Buttons that move between pages. Read-only: found, never clicked.
#
# "submit" is deliberately absent. `:has-text()` is a substring match, so it hit
# a dashboard column header reading "Submitted" and the walk clicked it. Submit
# is also the terminal action of a form, not a step through it: the crawl stops
# before it rather than pressing it.
_NEXT_PATTERNS = ["next", "continue", "save and continue", "get quote"]
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
    """Locator for the first visible button whose whole text is one of `patterns`.

    The match is on the button's *entire* text, not a substring: `:has-text()`
    matched a dashboard column header reading "Submitted" against the pattern
    "submit", and the walk clicked it. It would equally match "Next Steps"
    against "next".

    The emitted locator carries the button's visible text, so a "Next" button
    yields `button:has-text("Next")` as the architecture spec documents.
    """
    for word in patterns:
        # An anchored regex: Playwright's `exact=True` and `:text-is()` are both
        # case-sensitive, and the patterns are lowercase.
        whole = re.compile(rf"^\s*{re.escape(word)}\s*$", re.IGNORECASE)
        try:
            loc = page.get_by_role("button", name=whole)
            if loc.count() == 1 and loc.is_visible():
                return _prefer_visible_text(page, loc, f'button:has-text("{word}")')
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


def _measure_options(page: Page, options: list[dict] | None) -> list[dict] | None:
    """Resolve each option's own locator, where the option is a clickable node.

    A native `<option>` is set by label against its select and carries
    `locator: null`; a radio is clicked directly, so its candidates are measured
    the same way a control's are.
    """
    if not options:
        return options
    measured = []
    for opt in options:
        candidates = opt.get("candidates")
        if candidates:
            locator, _ = _first_unique(page, candidates)
            measured.append({"label": opt.get("label", ""), "locator": locator})
        else:
            measured.append({"label": opt.get("label", ""), "locator": opt.get("locator")})
    return measured


def _measure_actions(page: Page, actions: list[dict]) -> list[dict]:
    """Verify each clickable element's locator, dropping those with none."""
    out = []
    for a in actions:
        locator, unique = _first_unique(page, a.get("candidates", []))
        if not locator:
            continue
        out.append(
            {"label": a.get("text", ""), "href": a.get("href", ""),
             "locator": locator, "unique": unique}
        )
    return out


def _check_integrity(page: Page, controls: list[dict]) -> None:
    """Log the ways a control set can be structurally wrong but still validate.

    Each of these produces a well-formed PageDescription that misdirects every
    agent downstream, and none is visible once the payload leaves this module.
    """
    for c in controls:
        if not c.get("unique") and c.get("locator"):
            log.error(
                "locator is not unique: key=%s name=%r locator=%r",
                c.get("key"),
                c.get("name"),
                c["locator"],
            )

    seen: dict[str, str] = {}
    for c in controls:
        loc = c.get("locator")
        if not loc:
            continue
        if loc in seen:
            log.error(
                "duplicate locator %r on key=%s and key=%s; one of them addresses the wrong node",
                loc,
                seen[loc],
                c.get("key"),
            )
        seen[loc] = c.get("key", "")

    # A control whose locator resolves to several same-named radios is a radio
    # group the extractor failed to collapse: the question is lost and every
    # choice looks like its own field.
    for c in controls:
        loc = c.get("locator")
        if not loc or c.get("options"):
            continue
        try:
            handles = page.locator(loc).element_handles()
        except PlaywrightError:
            continue
        names = {h.get_attribute("name") for h in handles if h.get_attribute("type") == "radio"}
        if len(handles) > 1 and len(names) == 1 and None not in names:
            log.error(
                "ungrouped radio group: key=%s locator=%r matches %d inputs named %r",
                c.get("key"),
                loc,
                len(handles),
                next(iter(names)),
            )

    unlabelled = [c.get("key") for c in controls if not c.get("accessibleName")]
    if unlabelled:
        log.warning("controls with no accessible name: %s", ", ".join(map(str, unlabelled)))


_TOOLTIP_SETTLE_MS = 400
"""How long a hovered icon is given to mount its tooltip."""

_TOOLTIP_SELECTOR = '[role="tooltip"], .MuiTooltip-tooltip, [data-tooltip-content]'


def _read_help_tooltip(page: Page, key: str) -> str:
    """Hover the help icon the extractor tagged for `key` and read what mounts.

    The tooltip counterpart of opening a combobox to read its options: the text
    exists in the DOM only while the icon is hovered, so a static extraction
    never sees it, and Pie states the FEIN rule nowhere else. Read once per
    control per perceive, then the mouse is parked so the tooltip unmounts.

    A hover must not change the page. The URL is compared before and after; a
    move that navigated means the tag landed on a link, and its text is not a
    tooltip.
    """
    before = page.url
    trigger = page.locator(f'[data-tb-help="{key}"]')
    try:
        trigger.first.hover(timeout=2_000)
        page.wait_for_timeout(_TOOLTIP_SETTLE_MS)
        tips = page.locator(_TOOLTIP_SELECTOR)
        text = ""
        for i in range(tips.count()):
            t = tips.nth(i).inner_text().strip()
            if t:
                text = t
                break
    except PlaywrightError as e:
        log.debug("help icon for %s could not be hovered: %s", key, e)
        text = ""
    finally:
        try:
            page.mouse.move(0, 0)
            page.wait_for_timeout(150)
        except PlaywrightError:
            pass
    if page.url != before:
        log.warning("hovering the help icon for %s navigated the page; text discarded", key)
        return ""
    if text:
        log.info("help tooltip read key=%s chars=%d", key, len(text))
    return " ".join(text.split())[:400]


def _untag_help_triggers(page: Page) -> None:
    """Remove the extractor's `data-tb-help` marks so nothing leaks into later looks."""
    try:
        page.evaluate(
            "() => document.querySelectorAll('[data-tb-help]')"
            ".forEach((n) => n.removeAttribute('data-tb-help'))"
        )
    except PlaywrightError as e:
        log.debug("could not remove help-trigger tags: %s", e)


class DomSnapshotPerceiver:
    """DOM extraction for addressability, accessibility snapshot for semantics."""

    def perceive(self, page: Page) -> dict[str, Any]:
        """Extract controls, verify each locator, and attach the a11y tree."""
        payload: dict[str, Any] = page.evaluate(_EXTRACT_JS)
        raw = payload["controls"]
        log.debug(
            "extractor returned %d controls, %d actions", len(raw), len(payload["actions"])
        )

        controls = []
        for item in raw:
            locator, unique = _first_unique(page, item.get("candidates", []))
            cleaned = {k: v for k, v in item.items() if k not in ("candidates", "helpTrigger")}
            cleaned["options"] = _measure_options(page, item.get("options"))
            cleaned["helpText"] = (
                _read_help_tooltip(page, item["key"]) if item.get("helpTrigger") else ""
            )
            controls.append({**cleaned, "locator": locator, "unique": unique})
        _untag_help_triggers(page)

        _check_integrity(page, controls)

        return {
            "url": page.url,
            "title": page.title(),
            "controls": controls,
            "actions": _measure_actions(page, payload["actions"]),
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
            "actions": [],
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
