"""
Deciding what to type into a field.

This is the one genuine judgment call in the whole walk. Frontier's Assignment
names a control and carries no value, because Frontier never sees the element;
FormFiller holds it, so FormFiller decides.

Judgment is why it's a model and not a rule table. The deterministic fallback in
shared/values.py works by sniffing the label for substrings somebody already
wrote a branch for — "zip", "fein", "email". Everything else falls through to
"Test Value": "Suite / Unit", "NAICS code", "Years in business", "Number of
Members" all get a string a real form rejects. A model reads the label the way a
person filling the form would.

Cost is bounded by caching on (label, type). A chooser is re-filled once per
option walked, and a multi-page form repeats fields like Zip on every page, so
the same field is asked for many times over one walk and paid for once.
"""

import logging
import os
from typing import Protocol

from pydantic import BaseModel, Field

from trailblazer.contracts import Control, ControlState
from trailblazer.shared.llm import get_model
from trailblazer.shared.values import synthetic_value

logger = logging.getLogger(__name__)

_SYSTEM = """You fill out insurance application forms with realistic test data.

Given one form field, return the single value a real applicant would type into
it. The value has to be one the field will actually accept:

- Match the format the label implies: a ZIP is 5 digits, a FEIN is 12-3456789,
  a phone is 10 digits, an email is a valid address, a date field wants
  MM/DD/YYYY, a number field wants digits only -- no units, no separators, no
  currency symbol.
- Respect the constraints you are given (pattern, max length, placeholder).
- Use plausible US small-business data. A bakery in New York, not lorem ipsum,
  and not a real company's real details.
- Return the value only. No quotes, no explanation, no units, no label.

If the field is free text with no format implied, a short realistic phrase is
correct: "Harbor Point Bistro", not "Test Value"."""


class ValuePicker(Protocol):
    """
    What FormFiller calls to decide what to type.

    Wider than `shared.values.ValueProvider`, which takes the Control alone: a
    live element also carries constraints (pattern, maxLength, placeholder) and
    sits on a page with a heading, and all of that changes the right answer.
    `rule_based` below adapts the narrow one to this shape.
    """

    def __call__(
        self,
        control: Control | ControlState,
        context: str = "",
        constraints: dict | None = None,
    ) -> str: ...


def rule_based(
    control: Control | ControlState,
    context: str = "",
    constraints: dict | None = None,
) -> str:
    """`shared.values.synthetic_value`, widened to the ValuePicker signature."""
    return synthetic_value(control)


class FieldValue(BaseModel):
    """What the model returns for one field."""

    value: str = Field(description="The literal text to type into the field")


class LLMValuePicker:
    """
    A `ValuePicker` backed by a chat model, degrading to the rule table.

    Interchangeable with `rule_based` — same call signature — so the filler
    neither knows nor cares which one it is holding, and tests inject a third.
    """

    def __init__(
        self, model=None, cache: dict[tuple[str, str], str] | None = None
    ) -> None:
        self._model = model
        self._cache: dict[tuple[str, str], str] = {} if cache is None else cache
        self._warned = False

    @staticmethod
    def is_configured() -> bool:
        """True when a provider key is actually present in the environment."""
        provider = os.getenv("LLM_PROVIDER", "openrouter").lower()
        key = "OPENROUTER_API_KEY" if provider == "openrouter" else "ANTHROPIC_API_KEY"
        return bool(os.getenv(key))

    def _structured(self):
        if self._model is None:
            self._model = get_model(temperature=0.0).with_structured_output(FieldValue)
        return self._model

    def __call__(
        self,
        control: Control | ControlState,
        context: str = "",
        constraints: dict | None = None,
    ) -> str:
        """
        Pick a value for `control`.

        `context` is the page's heading when the caller has it — "Business
        Information" tells the model that "Name" means the business's and not a
        person's, which the label alone does not.
        """
        key = (control.label.strip().lower(), control.type)
        if key in self._cache:
            return self._cache[key]

        value = self._ask(control, context, constraints or {})
        self._cache[key] = value
        return value

    def _ask(
        self, control: Control | ControlState, context: str, constraints: dict
    ) -> str:
        if not self.is_configured():
            # Not an error: running offline is a legitimate way to use the
            # filler. Say it once rather than once per field.
            if not self._warned:
                logger.info("no LLM provider key set; falling back to rule-based values")
                self._warned = True
            return synthetic_value(control)

        try:
            result = self._structured().invoke(
                [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _describe(control, context, constraints)},
                ]
            )
            value = (result.value or "").strip()
        except Exception as e:
            # A model outage must not end a walk. The rule table is worse, not
            # broken, so degrade to it and carry on.
            logger.warning(
                "value model failed for %r (%s); using fallback", control.label, e
            )
            return synthetic_value(control)

        if not value:
            logger.warning(
                "value model returned nothing for %r; using fallback", control.label
            )
            return synthetic_value(control)

        logger.info("value for %r (%s) -> %r", control.label, control.type, value)
        return value


def _describe(
    control: Control | ControlState, context: str, constraints: dict
) -> str:
    """The one field, as the model sees it."""
    lines = [
        f"Form: {context}" if context else "Form: insurance application",
        f"Field label: {control.label}",
        f"Input type: {control.type}",
        f"Required: {control.required}",
    ]
    for name, value in constraints.items():
        if value:
            lines.append(f"{name.replace('_', ' ').capitalize()}: {value}")
    return "\n".join(lines)
