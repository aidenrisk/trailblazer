"""The Scraper agent: one look at one page, returned as a `ScraperResult`.

The model is asked only for judgment -- clean labels, the type enum, `required`
when the attribute is absent, and `blockers`. Identity and bookkeeping are
assigned in Python afterwards, because models get counters wrong and a fixed
rule applies more reliably in code than in a prompt.
"""

import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from langchain.agents import create_agent
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.agents.browser.tools import read_only_tools
from trailblazer.agents.scraper.diff import diff_pages
from trailblazer.agents.scraper.perceive import get_perceiver, payload_to_text
from trailblazer.agents.vision.vision import restore as vision_restore
from trailblazer.contracts.page_description import (
    Action,
    Control,
    Overlay,
    OverlayClickable,
    PageDescription,
)
from trailblazer.contracts.scraper_result import PerceiveRequest, ScraperResult
from trailblazer.observability.cost import CostTracker
from trailblazer.observability.ledger import RunLedger
from trailblazer.observability.logging import get_logger
from trailblazer.shared.config import Settings, get_settings
from trailblazer.shared.models import get_model, invoke_with_retry

log = get_logger(__name__)

_SYSTEM_PROMPT = (
    Path(__file__).parents[2] / "prompts" / "scraper" / "system.md"
).read_text()

# URL path segments that identify a routing scheme rather than a page.
_NOISE_SEGMENTS = {"app", "apps", "form", "forms", "page", "pages", "step", "steps", "v1", "v2"}


def _slugify(text: str) -> str:
    """Lowercase, non-alphanumerics to underscores, collapsed and trimmed."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


def derive_stage_slug(url: str, title: str) -> str:
    """Name the page: last meaningful URL segment, else the heading.

    The slug must be stable across revisits -- that is what lets Frontier
    recognise a page it has already walked.
    """
    segments = [s for s in urlparse(url).path.split("/") if s]
    for segment in reversed(segments):
        slug = _slugify(segment.rsplit(".", 1)[0])  # drop a .html extension
        if slug and slug not in _NOISE_SEGMENTS and not slug.isdigit():
            return slug
    return _slugify(title) or "page"


def finalize(
    page: PageDescription,
    page_index: int,
    url: str,
    title: str,
    actions: list[dict] | None = None,
) -> PageDescription:
    """Assign the fields code owns: `fieldId`, `stageId` and `actions`.

    `actions` are measured, so they are restored from the extractor payload for
    the same reason locators are: a model-authored click target is unverified.
    """
    for i, control in enumerate(page.controls, start=1):
        control.fieldId = f"q_{i:03d}"

    page.stageId = f"form_page_{page_index}_{derive_stage_slug(url, title)}"
    page.url = url
    if actions is not None:
        page.actions = [Action(**a) for a in actions]
    return page


def restore_measured_locators(
    described: PageDescription, payload_controls: list[dict]
) -> PageDescription:
    """Overwrite the model's `locator`/`unique` with the measured ones.

    The perceiver established each locator by `count() == 1` against the live
    page. Those values then travel through the model as text, so a model that
    rewrites or reformats one silently breaks the contract every downstream
    component depends on. The measurement wins; the prompt is not the
    enforcement mechanism.

    Matching is by the payload's per-element `key`, which `Control.key` makes
    required so a model that drops it fails parsing rather than arriving here.
    Should one still arrive keyless, the fallbacks are, in order:

    1. the model's own returned `locator`, matched against the payload's
       locator set -- an exact hit is real evidence of which entry is meant;
    2. position, but only when the response carries positive evidence it kept
       the payload's order -- see `_positional_is_safe`. Index alone is not
       evidence: a reordered or invented response paired by index gives every
       control a different field's locator.

    A control that survives all three is left as returned and logged. A wrong
    pairing is never produced silently: each wrong locator still resolves to
    exactly one node, so no downstream uniqueness check would catch it.
    """
    by_key = {c["key"]: c for c in payload_controls if c.get("key")}
    by_locator = {c["locator"]: c for c in payload_controls}

    if len(described.controls) != len(payload_controls):
        log.warning(
            "model returned %d controls for %d payload entries; "
            "locators restored only where a payload entry matched",
            len(described.controls),
            len(payload_controls),
        )

    positional_ok = _positional_is_safe(described, payload_controls)

    for i, control in enumerate(described.controls):
        source = by_key.get(control.key) or by_locator.get(control.locator)
        if source is None and positional_ok and i < len(payload_controls):
            source = payload_controls[i]
        if source is None:
            log.warning(
                "no payload entry matched control %r (key=%r locator=%r); "
                "locator left as returned and NOT restored",
                control.label,
                control.key,
                control.locator,
            )
            continue
        if control.locator != source["locator"] or control.unique != source["unique"]:
            log.warning(
                "model returned locator=%r unique=%s for %r; "
                "restoring measured locator=%r unique=%s",
                control.locator,
                control.unique,
                control.label,
                source["locator"],
                source["unique"],
            )
        _set_measured(
            control,
            source["locator"],
            source["unique"],
            bool(source.get("disabled")),
            str(source.get("formatHints") or ""),
            str(source.get("helpText") or ""),
            bool(source.get("typeahead")),
            bool(source.get("additionalRow")),
            str(source.get("error") or ""),
        )

    for control in described.controls:
        if not control.unique:
            log.warning("locator is not unique: %r (%r)", control.locator, control.label)

    return described


def restore_measured_overlays(
    described: PageDescription, payload_overlays: list[dict]
) -> PageDescription:
    """Rebuild `overlays` from the measurement, keeping only the model's judgment.

    The model contributes `kind` and `dismissKey`; title, text, locator and the
    clickables are the extractor's. A `dismissKey` naming no measured clickable
    is dropped, so Loop never presses an address the model composed. A dialog
    the model left out is kept as `unknown`: it is still over the page.
    """
    judged = {o.key: o for o in described.overlays if o.key}
    rebuilt = []
    for source in payload_overlays:
        clickables = [OverlayClickable(**c) for c in source.get("clickables", [])]
        keys = {c.key for c in clickables if c.locator}
        verdict = judged.get(source["key"])
        kind = verdict.kind if verdict is not None else "unknown"
        dismiss = verdict.dismissKey if verdict is not None else None
        if dismiss is not None and dismiss not in keys:
            log.warning(
                "model named dismissKey=%r for dialog %r but no measured clickable has it; dropped",
                dismiss, source.get("title"),
            )
            dismiss = None
        if verdict is None:
            log.warning("model did not judge dialog %r; kept as unknown", source.get("title"))
        rebuilt.append(Overlay(
            key=source["key"], title=source.get("title", ""), text=source.get("text", ""),
            hasControls=bool(source.get("hasControls")), locator=source.get("locator", ""),
            clickables=clickables, kind=kind, dismissKey=dismiss,
        ))
    extra = set(judged) - {o["key"] for o in payload_overlays}
    if extra:
        log.warning("model returned dialogs the extractor did not see: %s; dropped", sorted(extra))
    described.overlays = rebuilt
    return described


def _positional_is_safe(described: PageDescription, payload_controls: list[dict]) -> bool:
    """True only when the response carries positive evidence it kept payload order.

    Index is the weakest possible join, so it demands evidence rather than the
    mere absence of proof against it. The evidence is agreement: every control
    whose returned locator *is* a measured one must already sit at that entry's
    index. A response that agrees nowhere -- keyless, with invented locators --
    supplies nothing, and pairing it by index would hand every control a
    different field's address that still resolves to exactly one node, so no
    downstream check would catch it. Unequal lengths cannot be walked in step
    at all.
    """
    if len(described.controls) != len(payload_controls):
        return False

    index_of = {c["locator"]: i for i, c in enumerate(payload_controls)}
    overlap = [(i, index_of[c.locator]) for i, c in enumerate(described.controls)
               if c.locator in index_of]

    if not overlap:
        log.warning(
            "model returned no recognisable key or locator for any control; "
            "refusing to match by position, which cannot be verified"
        )
        return False
    if any(returned_at != payload_at for returned_at, payload_at in overlap):
        log.warning(
            "model returned the payload's locators in a different order; "
            "refusing to match by position, which would mispair every control"
        )
        return False
    return True


def _set_measured(
    control: Control,
    locator: str,
    unique: bool,
    disabled: bool = False,
    format_hint: str = "",
    help_text: str = "",
    typeahead: bool = False,
    additional_row: bool = False,
    error: str = "",
) -> None:
    """Assign the measured fields, bypassing nothing the contract checks.

    Built from the live field values rather than `model_dump()`, because `key`
    is excluded from serialization and a dump would drop it -- revalidating the
    result would then fail on a field the object actually has.
    """
    fields = {name: getattr(control, name) for name in Control.model_fields}
    measured = {
        "locator": locator,
        "unique": unique,
        "disabled": disabled,
        "formatHint": format_hint,
        "helpText": help_text,
        "typeahead": typeahead,
        "additionalRow": additional_row,
        "error": error,
    }
    Control.model_validate({**fields, **measured})
    control.locator = locator
    control.unique = unique
    control.helpText = help_text
    control.typeahead = typeahead
    control.additionalRow = additional_row
    control.disabled = disabled
    control.formatHint = format_hint
    control.error = error


def perceive(
    page: Page,
    request: PerceiveRequest,
    settings: Settings | None = None,
    ledger: RunLedger | None = None,
) -> ScraperResult:
    """Look at `page`, describe it, and diff against `request.prior`.

    The verified extractor payload goes in the human message so the model has
    real locators in front of it; the read-only tools stay available for it to
    look again when the payload is thin. Whatever the model says about a
    locator is discarded afterwards in favour of the measurement.
    """
    settings = settings or get_settings()
    started = time.monotonic()
    log.info(
        "perceive start job_id=%s page_index=%s perceiver=%s",
        request.job_id,
        request.page_index,
        settings.scraper_perceiver,
    )

    progress = {"phase": "dom"}
    stop = threading.Event()
    threading.Thread(
        target=_watch_hang, args=(stop, progress, request.job_id, page), daemon=True
    ).start()
    try:
        payload = _run_perceiver(page, settings, request.prior, progress)
        progress["phase"] = "model"
        described = _describe(page, payload, request, settings)
    finally:
        stop.set()
    payload_controls = payload["controls"]
    described, total = described

    restore_measured_locators(described, payload_controls)
    # An address the vision fallback proved for this page, put back before the
    # diff runs: the extractor cannot measure these controls, so a fresh
    # description carries them empty and would undo work already done.
    vision_restore(page, described)
    restore_measured_overlays(described, payload.get("overlays", []))
    described.next = payload["next"]
    described.back = payload["back"]
    finalize(
        described,
        request.page_index,
        payload["url"],
        payload["title"],
        payload.get("actions"),
    )
    # Rejection text the extractor could tie to no field is a page blocker,
    # measured; the model's own list is kept alongside.
    for text in payload.get("pageErrors", []):
        if text not in described.blockers:
            described.blockers.append(text)

    scraper_result = diff_pages(described, request.prior, request.assignment)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "perceive end job_id=%s stage_id=%s controls=%d polarity=%s ms=%d",
        request.job_id,
        described.stageId,
        len(described.controls),
        scraper_result.polarity,
        elapsed_ms,
    )
    if ledger is not None:
        ledger.record(
            agent="scraper",
            action="perceive",
            detail=described.stageId,
            usd=total or 0.0,
            ms=elapsed_ms,
            unpriced=total is None,
        )
    return scraper_result


_HANG_WARN_S = 60
"""Seconds a look may run before the watchdog names the phase it is in."""


def _watch_hang(stop: threading.Event, progress: dict[str, str], job_id: str, page: Page) -> None:
    """Log, every `_HANG_WARN_S`, which phase a still-running look is in.

    A look froze for sixteen minutes on a run with a failing connection and
    the log showed only its start line. This says where it is -- the DOM
    extraction, the tooltip hovers, the a11y snapshot or the model call -- so
    the freeze is diagnosable while it happens.
    """
    while not stop.wait(_HANG_WARN_S):
        log.error(
            "look still running after %ds job_id=%s phase=%s url=%s",
            _HANG_WARN_S, job_id, progress.get("phase"), page.url,
        )


def _describe(
    page: Page, payload: dict, request: PerceiveRequest, settings: Settings
) -> tuple[PageDescription, float | None]:
    """Ask the model for its judgment on the payload. Returns the description and its cost."""
    text = payload_to_text(payload)
    log.debug(
        "extractor payload job_id=%s controls=%d bytes=%d",
        request.job_id, len(payload["controls"]), len(text),
    )
    if not payload["controls"]:
        log.warning(
            "extractor found no controls on %s; the page may not have rendered yet, "
            "or its inputs may live in a cross-origin iframe",
            payload["url"],
        )
    agent = create_agent(
        model=get_model(settings),
        tools=read_only_tools(page),
        system_prompt=_SYSTEM_PROMPT,
        response_format=PageDescription,
    )
    objective = request.objective or "Describe this form page."
    tracker = CostTracker(step="perceive", job_id=request.job_id)
    result = invoke_with_retry(
        lambda: agent.invoke(
            {"messages": [{"role": "user", "content": f"{objective}\n\nExtractor payload:\n{text}"}]},
            config={"callbacks": [tracker]},
        ),
        step="perceive",
    )
    total = tracker.total_usd()
    log.info(
        "perceive llm total job_id=%s calls=%d usd=%s",
        request.job_id, len(tracker.calls), "unknown" if total is None else f"{total:.6f}",
    )
    described = result.get("structured_response")
    if not isinstance(described, PageDescription):
        raise RuntimeError(
            "the model did not return a parseable PageDescription "
            f"(got {type(described).__name__}); the endpoint it routed to may not "
            "support structured output -- check OPENROUTER_MODEL"
        )
    return described, total


def _run_perceiver(
    page: Page,
    settings: Settings,
    prior: PageDescription | None = None,
    progress: dict[str, str] | None = None,
) -> dict:
    """Perceive, turning a failed in-page evaluate into a message that names the cause.

    `prior` supplies the help text already read for this page's controls, keyed
    by locator, so a re-look does not hover every icon again.
    """
    known_help = {c.locator: c.helpText for c in prior.controls if c.helpText} if prior else {}
    try:
        return get_perceiver(settings.scraper_perceiver).perceive(page, known_help, progress)
    except PlaywrightError as e:
        raise RuntimeError(
            f"reading the page failed: {e}. The tab may have been closed or navigated "
            "away mid-perceive, or a Content-Security-Policy may block script evaluation"
        ) from e
