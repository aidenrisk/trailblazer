"""Choosing what to type. The filler's one piece of judgment, and its only LLM call.

"Select Yes" carries its value in the assignment and needs no model. "Fill the
FEIN field" does not: something has to decide that a plausible FEIN is nine
digits, and that a plausible employee count for a contractor is 12 rather than
1 or 100000. That is judgment about the world, not about the DOM, which is why
it is the one thing here a model does.

The model is asked for a value and nothing else -- no tools, no structured
schema. A single string is the whole contract, so a response that wanders into
prose is trimmed to its first line rather than being parsed.
"""

import time
from datetime import date
from pathlib import Path

from trailblazer.observability.cost import CostTracker
from trailblazer.observability.logging import get_logger
from trailblazer.shared.config import Settings
from trailblazer.shared.models import _ATTEMPTS, TransientModelError, get_model, invoke_with_retry

log = get_logger(__name__)

_SYSTEM_PROMPT = (
    Path(__file__).parents[2] / "prompts" / "form_filler" / "system.md"
).read_text()


def choose_value(
    label: str,
    locator: str,
    url: str,
    constraint_hint: str | None,
    error_text: str | None,
    settings: Settings,
    business_type: str = "",
    state: str = "",
    control_type: str = "",
    help_text: str | None = None,
    options: list[str] | None = None,
) -> tuple[str, float, bool]:
    """Decide what to type into one field.

    Returns the value, the USD the call cost, and whether it could not be
    priced. The cost is returned rather than logged and forgotten because the
    caller records it on the run ledger, and a step that does not report its
    spend is invisible in the per-agent accounting.

    `error_text` is the page's complaint about the previous attempt. When it is
    given the model is correcting a rejected value, not choosing a first one.
    """
    started = time.monotonic()
    model = get_model(settings)
    tracker = CostTracker(step="choose_value")

    # Today's date is sent because the model has no clock: without it a policy
    # effective date came back in the past, which every carrier rejects. The
    # state and business type scope the values the same way -- a ZIP from the
    # wrong state walks a path the flow does not cover.
    lines = [
        f"Field: {label}",
        f"Locator: {locator}",
        f"Page: {url}",
        f"Today's date: {date.today().isoformat()}",
    ]
    if state:
        lines.append(f"State the crawl is scoped to: {state}")
    if business_type:
        lines.append(f"Business type: {business_type}")
    if control_type:
        lines.append(f"Control type: {control_type}")
    if constraint_hint:
        lines.append(f"What the page says about the format: {constraint_hint}")
    if help_text:
        lines.append(f"What the field's help tooltip says: {help_text}")
    if options:
        lines.append("The field offers exactly these choices; return one of them verbatim:")
        lines.extend(f"  - {o}" for o in options)
    if error_text:
        lines.append(f"The page rejected the previous value with: {error_text}")
        lines.append("Return a corrected value that satisfies it.")

    def ask() -> str:
        response = model.invoke(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(lines)},
            ],
            config={"callbacks": [tracker]},
        )
        chosen = _first_line(response.content)
        if not chosen:
            # An empty reply is the provider's failure, not the field's: asked
            # again it usually answers. Raised as transient so the retry sees it.
            raise TransientModelError(f"the model returned no value for field {label!r}")
        return chosen

    try:
        value = invoke_with_retry(ask, step="choose_value")
    except TransientModelError as e:
        raise RuntimeError(
            f"{e} after {_ATTEMPTS} attempts; "
            "the endpoint may be refusing the request -- check OPENROUTER_MODEL"
        ) from e

    total = tracker.total_usd()
    log.info(
        "chose value field=%s corrected=%s usd=%s ms=%d",
        label,
        error_text is not None,
        "unknown" if total is None else f"{total:.6f}",
        int((time.monotonic() - started) * 1000),
    )
    return value, (0.0 if total is None else total), total is None


def _first_line(content) -> str:
    """The value out of a response that may be a string or a content-block list.

    Only the first non-empty line is kept: the prompt asks for the value alone,
    and a model that adds "This is a valid FEIN." on a second line would
    otherwise have that typed into the field.
    """
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    for line in str(content).splitlines():
        stripped = line.strip().strip('"').strip()
        if stripped:
            return stripped
    return ""
