"""The fallback that looks at the page when the deterministic path is exhausted.

Reached only where code has nothing left to try: a control the extractor could
not address, a forward press the page refused without naming a field, a fill
that reported success and changed nothing. In every one of those the page still
says on screen what it wants -- a row reading "2025-26", a message reading
"select at least one term" -- and nothing in the markup connects those words to
the element they belong to.

So the elements are badged with numbers, the page is photographed once, and the
model is asked what words sit near each badge. It answers with text, never with
a selector. Python turns the text into candidate locators, measures them the way
every other locator is measured, and then does the thing the earlier structural
guess could not: proves the locator resolves to the *same DOM node* that carried
the badge. A misread costs a rejected candidate; it cannot produce a confident
address for the wrong element.

One call per invocation, bounded per page. Where it yields nothing the caller
stops loudly -- this is the last resort, so its failure is the page's failure.
"""

import base64
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.contracts.page_description import Control
from trailblazer.contracts.vision import Anchor, VisionReading
from trailblazer.observability.cost import CostTracker
from trailblazer.observability.logging import get_logger, log_contract
from trailblazer.shared.config import Settings, get_settings
from trailblazer.shared.models import get_vision_model, invoke_with_retry

log = get_logger(__name__)

_BADGE_JS = (Path(__file__).parent / "badge.js").read_text()
_SYSTEM_PROMPT = (
    Path(__file__).parents[2] / "prompts" / "vision" / "system.md"
).read_text()

_UNBADGE_JS = """
() => {
  document.getElementById('tb-badges')?.remove();
  document.querySelectorAll('[data-tb-badge]').forEach((n) => n.removeAttribute('data-tb-badge'));
}
"""

_MAX_BADGES = 25
"""Elements one screenshot may carry. Past this the picture is unreadable and
the numbers overlap; the caller's page has a different problem than addressing."""


def read_page(
    page: Page,
    controls: list[Control],
    question: str,
    settings: Settings | None = None,
    job_id: str | None = None,
) -> VisionReading:
    """Badge `controls`, photograph the page once, and ask what is written near each.

    `question` is what the caller could not answer from the DOM -- "which of
    these does the page's error refer to", "what identifies each of these" --
    and is put to the model alongside the standing instructions.

    The badges are removed before returning, whatever happens: they are drawn in
    a fixed overlay and stamped as an attribute, and a page left marked would be
    photographed marked on the next look.
    """
    settings = settings or get_settings()
    started = time.monotonic()

    keys = [c.key for c in controls if c.key][:_MAX_BADGES]
    if not keys:
        log.warning("vision asked to read no addressable elements; nothing to badge")
        return VisionReading()

    try:
        badges = page.evaluate(_BADGE_JS, keys)
        if not badges:
            log.warning("none of the %d elements offered to vision are visible", len(keys))
            return VisionReading()
        shot = page.screenshot(type="png")
    except PlaywrightError as e:
        log.error("vision could not photograph the page: %s", e)
        return VisionReading()
    finally:
        _unbadge(page)

    log.info(
        "vision looking job_id=%s badges=%d bytes=%d question=%r",
        job_id, len(badges), len(shot), question[:80],
    )
    reading = _ask(shot, badges, question, settings, job_id)
    log_contract(log, "VisionReading", reading)
    log.info(
        "vision read job_id=%s anchors=%d relevant=%s ms=%d",
        job_id, len(reading.anchors), reading.relevant,
        int((time.monotonic() - started) * 1000),
    )
    return reading


def resolve(page: Page, controls: list[Control], reading: VisionReading) -> dict[str, str]:
    """Turn each anchor's words into a locator, and keep only the ones that hit.

    Returns `key -> locator` for the elements whose address was proven. A
    candidate is kept only when it matches exactly one node on the page *and*
    that node is the one the badge was drawn on -- `data-tb-badge` is still
    stamped for the duration of this call, so identity is checkable rather than
    inferred. That check is the whole reason this can be trusted where the
    structural guess it replaces could not: there, a wrong address still
    resolved to one node and passed.
    """
    if not reading.anchors:
        return {}

    by_badge = _restamp(page, controls, reading)
    if not by_badge:
        return {}

    resolved: dict[str, str] = {}
    try:
        for anchor in reading.anchors:
            key = by_badge.get(anchor.badge)
            if key is None:
                log.warning("vision named badge %d, which was never drawn", anchor.badge)
                continue
            control = next((c for c in controls if c.key == key), None)
            if control is None:
                continue
            locator = _first_hit(page, anchor, control, anchor.badge)
            if locator is None:
                log.warning(
                    "no locator built from %r/%r reaches badge %d (%s)",
                    anchor.label, anchor.heading, anchor.badge, control.fieldId,
                )
                continue
            resolved[key] = locator
            log.info(
                "vision addressed %s via %r: %s",
                control.fieldId, anchor.label or anchor.heading, locator,
            )
    finally:
        _unbadge(page)
    return resolved


def _restamp(page: Page, controls: list[Control], reading: VisionReading) -> dict[int, str]:
    """Re-draw the badges so `resolve` can check identity. Returns badge -> key."""
    keys = [c.key for c in controls if c.key][:_MAX_BADGES]
    try:
        return {b["badge"]: b["key"] for b in page.evaluate(_BADGE_JS, keys)}
    except PlaywrightError as e:
        log.error("vision could not re-badge the page to verify: %s", e)
        return {}


def _first_hit(page: Page, anchor: Anchor, control: Control, badge: int) -> str | None:
    """The first candidate that resolves to exactly the element carrying `badge`.

    Candidates run from most to least specific: the label and heading together,
    then the label alone. Each is expressed as "the element of this kind nearest
    the text", which is what the model actually saw, and each is tested by
    identity rather than by count alone.
    """
    for selector in _candidates(anchor, control):
        try:
            found = page.locator(selector)
            if found.count() != 1:
                continue
            if found.first.get_attribute("data-tb-badge") == str(badge):
                return selector
            log.debug("candidate %r resolves to a different element than badge %d", selector, badge)
        except PlaywrightError as e:
            log.debug("candidate %r rejected: %s", selector, e)
    return None


def _candidates(anchor: Anchor, control: Control) -> list[str]:
    """Locators expressing "the control of this kind nearest these words".

    The text is the model's, quoted into the selector; the structure around it
    is ours. An anchor cell is found by its exact text, then the search walks up
    to the nearest ancestor holding a control of this kind and takes the one at
    the position the DOM order gives. Where a heading is also given, the pairing
    of row text and column text narrows a grid to one cell.
    """
    tag = "select" if control.type == "select" and control.options else "input"
    kind = _kind(control)
    out: list[str] = []

    label = anchor.label.strip()
    heading = anchor.heading.strip()

    def usable(text: str) -> bool:
        return len(text) >= 2 and '"' not in text

    if usable(label) and usable(heading):
        # Row and column: the cell that reads the label, in the row that also
        # holds the heading's column position. Expressed as an ancestor walk so
        # it works for a table and for a grid of divs alike.
        out.append(
            f'xpath=//*[normalize-space(text())="{label}"]'
            f'/ancestor::*[.//{tag}][1]//{tag}{kind}'
        )
    if usable(label):
        out.append(
            f'xpath=//*[normalize-space(text())="{label}"]'
            f'/following::{tag}{kind}[1]'
        )
        out.append(
            f'xpath=//*[normalize-space(text())="{label}"]'
            f'/ancestor::*[.//{tag}][1]//{tag}{kind}'
        )
        out.append(f'{tag}{_css_kind(control)} >> internal:label="{label}"i')
    # A heading alone is never an address. "the first checkbox after the column
    # header" names a position, not a field: on a fixture it resolved to the
    # right element for the wrong reason, and one re-ordered row would send the
    # replay script to a different answer. The heading qualifies a label; it
    # does not stand in for one.
    return out


def _kind(control: Control) -> str:
    """The xpath predicate narrowing to this control's input type.

    Only a toggle narrows: a checkbox sits beside other inputs in the same row,
    and without the predicate "the input after this text" reaches a text box.
    Every other type takes the nearest input of any kind, which is what the
    model saw.
    """
    return '[@type="checkbox" or @type="radio"]' if control.type == "toggle" else ""


def _css_kind(control: Control) -> str:
    """The CSS equivalent of `_kind`, for the label-engine candidate."""
    return '[type="checkbox"], input[type="radio"]' if control.type == "toggle" else ""


def _unbadge(page: Page) -> None:
    """Remove every badge and mark. A page left marked is photographed marked."""
    try:
        page.evaluate(_UNBADGE_JS)
    except PlaywrightError as e:
        log.debug("could not remove badges: %s", e)


def _ask(
    shot: bytes,
    badges: list[dict],
    question: str,
    settings: Settings,
    job_id: str | None,
) -> VisionReading:
    """One model call: the screenshot, the badge table, and what the caller needs."""
    table = "\n".join(
        f"badge {b['badge']}: {b['tag']}"
        + (f" type={b['inputType']}" if b["inputType"] else "")
        + f" at ({b['rect']['x']},{b['rect']['y']}) {b['rect']['w']}x{b['rect']['h']}"
        for b in badges
    )
    model = get_vision_model(settings).with_structured_output(VisionReading)
    tracker = CostTracker(step="vision", job_id=job_id)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": f"{question}\n\nBadges drawn on the page:\n{table}"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{base64.b64encode(shot).decode()}"},
            },
        ],
    }
    reading = invoke_with_retry(
        lambda: model.invoke(
            [{"role": "system", "content": _SYSTEM_PROMPT}, message],
            config={"callbacks": [tracker]},
        ),
        step="vision",
    )
    total = tracker.total_usd()
    log.info(
        "vision llm total job_id=%s calls=%d usd=%s",
        job_id, len(tracker.calls), "unknown" if total is None else f"{total:.6f}",
    )
    if not isinstance(reading, VisionReading):
        raise RuntimeError(
            f"the vision model did not return a parseable VisionReading (got "
            f"{type(reading).__name__}); check VISION_MODEL accepts images and "
            "structured output"
        )
    return reading
