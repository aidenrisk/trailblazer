"""The Scraper agent: one look at one page, returned as a `ScraperResult`.

The model is asked only for judgment -- clean labels, the type enum, `required`
when the attribute is absent, and `blockers`. Identity and bookkeeping are
assigned in Python afterwards, because models get counters wrong and a fixed
rule applies more reliably in code than in a prompt.
"""

import re
import time
from pathlib import Path
from urllib.parse import urlparse

from langchain.agents import create_agent
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from pydantic import Field

from trailblazer.agents.browser.tools import read_only_tools
from trailblazer.agents.scraper.diff import diff_pages
from trailblazer.agents.scraper.perceive import get_perceiver, payload_to_text
from trailblazer.contracts.page_description import Control, Option, PageDescription, RevealedBy
from trailblazer.contracts.scraper_result import PerceiveRequest, ScraperResult
from trailblazer.observability.cost import CostTracker
from trailblazer.observability.logging import get_logger
from trailblazer.shared.config import Settings, get_settings
from trailblazer.shared.models import get_model

log = get_logger(__name__)

_SYSTEM_PROMPT = (
    Path(__file__).parents[2] / "prompts" / "scraper" / "system.md"
).read_text()


class _ModelControl(Control):
    """The shape the model must return. Same contract, stricter schema.

    The shared `Control` gives `key`, `options` and `revealedBy` defaults so a
    page can be built by hand. The model gets none of those: every field lands
    in the JSON schema's `required` list, so a response that drops `key` fails
    structured-output parsing loudly instead of leaving the locator join to
    guesswork.
    """

    key: str = Field(exclude=True)
    options: list[Option] | None
    revealedBy: RevealedBy | None


class _ModelPage(PageDescription):
    """`PageDescription` with every field required, for the model's response format."""

    controls: list[_ModelControl]
    next: str | None
    back: str | None
    candidateGates: list[str]
    blockers: list[str]

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


def finalize(page: PageDescription, page_index: int, url: str, title: str) -> PageDescription:
    """Assign the three things code owns: `fieldId`, `stageId`, `candidateGates`.

    `candidateGates` is every control with a non-empty `options` list. It
    over-reports -- a 50-state dropdown becomes a candidate -- but Frontier
    settles that by walking, and a false candidate costs one wasted walk while a
    missed gate costs an unexplored branch. Wrong in the cheap direction.
    """
    for i, control in enumerate(page.controls, start=1):
        control.fieldId = f"q_{i:03d}"

    page.stageId = f"form_page_{page_index}_{derive_stage_slug(url, title)}"
    page.url = url
    page.candidateGates = [c.fieldId for c in page.controls if c.options]
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
        _set_measured(control, source["locator"], source["unique"])

    for control in described.controls:
        if not control.unique:
            log.warning("locator is not unique: %r (%r)", control.locator, control.label)

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


def _set_measured(control: Control, locator: str, unique: bool) -> None:
    """Assign a validated locator, bypassing nothing the contract checks.

    Built from the live field values rather than `model_dump()`, because `key`
    is excluded from serialization and a dump would drop it -- revalidating the
    result would then fail on a field the object actually has.
    """
    fields = {name: getattr(control, name) for name in Control.model_fields}
    Control.model_validate({**fields, "locator": locator, "unique": unique})
    control.locator = locator
    control.unique = unique


def perceive(page: Page, request: PerceiveRequest, settings: Settings | None = None) -> ScraperResult:
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

    payload = _run_perceiver(page, settings)
    payload_controls = payload["controls"]
    text = payload_to_text(payload)
    log.debug(
        "extractor payload job_id=%s controls=%d bytes=%d",
        request.job_id,
        len(payload_controls),
        len(text),
    )
    if not payload_controls:
        log.warning(
            "extractor found no controls on %s; the page may not have rendered yet, "
            "or its inputs may live in a cross-origin iframe",
            payload["url"],
        )

    agent = create_agent(
        model=get_model(settings),
        tools=read_only_tools(page),
        system_prompt=_SYSTEM_PROMPT,
        response_format=_ModelPage,
    )

    objective = request.objective or "Describe this form page."
    tracker = CostTracker(step="perceive", job_id=request.job_id)
    result = agent.invoke(
        {"messages": [{"role": "user", "content": f"{objective}\n\nExtractor payload:\n{text}"}]},
        config={"callbacks": [tracker]},
    )

    total = tracker.total_usd()
    log.info(
        "perceive llm total job_id=%s calls=%d usd=%s",
        request.job_id,
        len(tracker.calls),
        "unknown" if total is None else f"{total:.6f}",
    )

    described = result.get("structured_response")
    if not isinstance(described, PageDescription):
        raise RuntimeError(
            "the model did not return a parseable PageDescription "
            f"(got {type(described).__name__}); the endpoint it routed to may not "
            "support structured output -- check OPENROUTER_MODEL"
        )

    restore_measured_locators(described, payload_controls)
    described.next = payload["next"]
    described.back = payload["back"]
    finalize(described, request.page_index, payload["url"], payload["title"])

    scraper_result = diff_pages(described, request.prior, request.assignment)
    log.info(
        "perceive end job_id=%s stage_id=%s controls=%d polarity=%s ms=%d",
        request.job_id,
        described.stageId,
        len(described.controls),
        scraper_result.polarity,
        (time.monotonic() - started) * 1000,
    )
    return scraper_result


def _run_perceiver(page: Page, settings: Settings) -> dict:
    """Perceive, turning a failed in-page evaluate into a message that names the cause."""
    try:
        return get_perceiver(settings.scraper_perceiver).perceive(page)
    except PlaywrightError as e:
        raise RuntimeError(
            f"reading the page failed: {e}. The tab may have been closed or navigated "
            "away mid-perceive, or a Content-Security-Policy may block script evaluation"
        ) from e
