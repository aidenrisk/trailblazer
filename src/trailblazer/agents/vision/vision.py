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
import logging
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.contracts.page_description import Control
from trailblazer.contracts.vision import Anchor, VisionReading
from trailblazer.observability.cost import CostTracker
from trailblazer.observability.events import event
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

_ADDRESSED: dict[str, dict[str, str]] = {}
"""stageId -> key -> the locator vision proved for it.

Kept for the run. The extractor re-measures every control on every look and
these have nothing to measure, so without this the next look overwrites a
proven address with the empty string it found -- on a live run vision addressed
eight controls, one was filled, and the following perceive discarded all eight.
Keyed on `Control.key`, the extractor's per-element id, which is stable for as
long as the page's markup is.
"""

_SHOT_DIR: Path | None = None
"""Where badged screenshots are written, set by `set_shot_dir`. Every picture
the model was shown is kept: a reading that misses is diagnosed from what it
saw, which the log line alone cannot carry."""


def set_shot_dir(path: Path) -> None:
    """Write screenshots into `path`. Called once, with the run's own folder."""
    global _SHOT_DIR
    _SHOT_DIR = path


def _save(shot: bytes, badges: list[dict]) -> str | None:
    """Write the badged screenshot beside the artifacts. Returns its path."""
    if _SHOT_DIR is None:
        return None
    _SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = _SHOT_DIR / f"vision-{int(time.time())}-{len(badges)}badges.png"
    path.write_bytes(shot)
    return str(path)


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

    shot_path = _save(shot, badges)
    event("vision", "vision", badges=len(badges), screenshot=shot_path, asked=question)
    by_key = {c.key: c for c in controls}
    for badge in badges:
        # What each number in the picture stands for, before the model says
        # anything: the reading below is judged against this, and a badge whose
        # own label is empty is why the model was asked at all.
        control = by_key.get(badge["key"])
        event(
            "vision", "vision", badge=badge["badge"], field=control.fieldId if control else None,
            label=(control.label if control else "") or None,
            type=control.type if control else None,
            tooltip=(control.helpText if control else "") or None,
            format=(control.formatHint if control else "") or None,
            tag=badge["tag"], at=f'{badge["rect"]["x"]},{badge["rect"]["y"]}',
            size=f'{badge["rect"]["w"]}x{badge["rect"]["h"]}',
        )
    reading = _ask(shot, badges, question, settings, job_id)
    log_contract(log, "VisionReading", reading)
    for anchor in reading.anchors:
        event(
            "vision", "vision", badge=anchor.badge, label=anchor.label,
            heading=anchor.heading or None, purpose=anchor.purpose or None,
        )
    event(
        "vision", "vision", anchors=len(reading.anchors),
        relevant=",".join(str(b) for b in reading.relevant) or None,
        note=reading.note or None, ms=int((time.monotonic() - started) * 1000),
    )
    return reading


def resolve(
    page: Page,
    controls: list[Control],
    reading: VisionReading,
    stage_id: str = "",
) -> dict[str, str]:
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
                event(
                    "vision", "vision", logging.WARNING, badge=anchor.badge,
                    field=control.fieldId, label=anchor.label, heading=anchor.heading or None,
                    resolved=False, tried=len(_candidates(anchor, control)),
                )
                continue
            resolved[key] = locator
            event(
                "vision", "vision", badge=anchor.badge, field=control.fieldId,
                via=anchor.label or anchor.heading, locator=locator, resolved=True,
            )
    finally:
        _unbadge(page)
    if resolved and stage_id:
        _ADDRESSED.setdefault(stage_id, {}).update(resolved)
    return resolved


def restore(page: Page, page_description) -> int:
    """Put back the addresses vision proved for this page. Returns how many.

    Called after every look. The extractor cannot measure these controls -- that
    is why vision ran -- so each fresh description carries them empty again and
    would undo the work. An address is only restored while it still resolves to
    exactly one node: a page that re-rendered differently gets no stale locator,
    it gets another look.
    """
    known = _ADDRESSED.get(page_description.stageId)
    if not known:
        return 0
    restored = 0
    for control in page_description.controls:
        locator = known.get(control.key)
        if not locator or control.locator:
            continue
        try:
            if page.locator(locator).count() != 1:
                log.warning(
                    "vision address for %s no longer resolves to one node; dropped: %s",
                    control.fieldId, locator,
                )
                continue
        except PlaywrightError as e:
            log.warning("vision address for %s rejected: %s", control.fieldId, e)
            continue
        control.locator = locator
        control.unique = True
        restored += 1
    if restored:
        event("vision", "vision", stage=page_description.stageId, restored=restored)
    return restored


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

    Every candidate is written at DEBUG as it is tried; if none hits, the whole
    attempt is written again at WARNING, one line per candidate, so a failure
    shows what was tried without needing DEBUG turned on for the entire run.
    """
    tried: list[dict] = []
    for selector in _candidates(anchor, control):
        try:
            found = page.locator(selector)
            matched = found.count()
            if matched != 1:
                event("vision", "vision", logging.DEBUG, badge=badge, field=control.fieldId,
                      candidate=selector, matched=matched, hit=False)
                tried.append({"candidate": selector, "matched": matched, "hit": False})
                continue
            on_badge = found.first.get_attribute("data-tb-badge") == str(badge)
            event("vision", "vision", logging.DEBUG, badge=badge, field=control.fieldId,
                  candidate=selector, matched=1, hit=on_badge)
            if on_badge:
                return selector
            tried.append({"candidate": selector, "matched": 1, "hit": False})
        except PlaywrightError as e:
            event("vision", "vision", logging.DEBUG, badge=badge, field=control.fieldId,
                  candidate=selector, hit=False, detail=str(e))
            tried.append({"candidate": selector, "hit": False, "detail": str(e)})
    for t in tried:
        event("vision", "vision", logging.WARNING, badge=badge, field=control.fieldId, **t)
    return None


def _candidates(anchor: Anchor, control: Control) -> list[str]:
    """Locators expressing "the control of this kind nearest these words".

    The text is the model's, quoted into the selector; the structure around it
    is ours. The label's cell is found by its exact text and the search runs
    forward to the nearest control *of this control's kind*. Kind is what
    separates two controls sharing a row: Pie's "2025-26" carries both a
    checkbox and a `role="listbox"` dropdown, and each finds its own.

    The heading is not part of any address. It reached the right element for
    the wrong reason on a fixture -- position, not identity -- and a re-ordered
    row would send the replay script to a different answer.
    """
    tag = "select" if control.type == "select" and control.options else "input"
    kind = _kind(control)
    out: list[str] = []

    label = anchor.label.strip()

    def usable(text: str) -> bool:
        return len(text) >= 2 and '"' not in text

    # `normalize-space(.)` with a childless guard, never `text()`: Pie renders a
    # row label as `<p>2025-26</p>`, whose text is a child node, so a `text()`
    # predicate matched nothing and every candidate for all eight controls was
    # rejected on a live run. The guard keeps the match on the leaf that reads
    # the words rather than every ancestor that contains them.
    def leaf(text: str) -> str:
        return f'//*[normalize-space(.)="{text}"][not(*)]'

    if usable(label):
        # The control of this kind nearest the label, in document order: for a
        # grid row and for a table row alike, the label's cell precedes its
        # inputs.
        out.append(f"xpath={leaf(label)}/following::{tag}{kind}[1]")
        # Failing that, the nearest ancestor holding one -- a row wrapper that
        # puts the label after its input, or a label element around both.
        out.append(f"xpath={leaf(label)}/ancestor::*[.//{tag}{kind}][1]//{tag}{kind}")
        out.append(f'{tag}{_css_kind(control)} >> internal:label="{label}"i')
    return out


def _kind(control: Control) -> str:
    """The xpath predicate narrowing to this control's kind.

    A row holds controls of different kinds -- Pie's lapse grid pairs a
    checkbox with a `role="listbox"` dropdown -- so "the input after this text"
    reaches whichever comes first and fails the identity check for the other.
    The predicate is what lets each find its own.
    """
    if control.type == "toggle":
        return '[@type="checkbox" or @type="radio"]'
    if control.typeahead or control.type in ("select", "other"):
        # A custom chooser: a text box the page marks as a list, which is how
        # Pie draws every dropdown that is not a native `<select>`.
        return '[@role="listbox" or @role="combobox"]'
    return '[not(@role="listbox") and not(@role="combobox") and not(@type="checkbox") and not(@type="radio")]'


def _css_kind(control: Control) -> str:
    """The CSS equivalent of `_kind`, for the label-engine candidate."""
    if control.type == "toggle":
        return '[type="checkbox"], input[type="radio"]'
    if control.typeahead or control.type in ("select", "other"):
        return '[role="listbox"], input[role="combobox"]'
    return ""


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
