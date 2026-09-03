"""Reconciliation rules applied as entries are appended (arch doc 4).

Four rules, each closing a named RoadRunner failure:

- Merge fields are stripped from labels. Nothing downstream substitutes
  `{business_address}`, so the braces shipped to the client.
- An option list holding placeholder prose is discarded and the control is
  marked `openSet` instead. A typeahead's help text stored as the sole option
  became that field's only accepted value.
- When one fact is captured more than once, requiredness is the OR across
  captures and the option list comes from the capture with the most real
  options. County appeared once required with a placeholder and once optional
  with all 58; taking either row whole loses half of it.
- The catalog type is derived from unit and option count, never from the HTML
  type alone: a dollar amount in a text box is money, a ZIP in a `tel` box is
  not a phone.
"""

import re

from trailblazer.contracts.page_description import Option

# `{business_address}` and `{{business_address}}`: a template the crawl never
# resolved and nothing downstream will.
_MERGE_FIELD = re.compile(r"\{\{?\s*[\w.]+\s*\}?\}")
_WHITESPACE = re.compile(r"\s+")

# Prose that a portal puts where an option belongs. A typeahead with no
# enumerable set renders its instructions as the only listbox row, and storing
# that row makes the instruction the field's only legal answer.
_PLACEHOLDER_PROSE = (
    "select",
    "choose",
    "start typing",
    "type to search",
    "search",
    "begin typing",
    "please select",
    "-- select",
    "none selected",
    "loading",
    "no results",
    "enter ",
)

# A canonical naming a postal code. `tel`/`phone` is the wrong catalog type for
# it: a ZIP in a `tel` box is not a phone number (arch doc 3.5).
_POSTAL_CANONICAL = re.compile(r"zip|postal|post_?code")

# A date control whose question asks for a year holds a number, not a date.
_YEAR_QUESTION = re.compile(r"\byear\b|\byr\b", re.IGNORECASE)


def strip_merge_fields(label: str) -> str:
    """Remove unresolved `{merge_field}` templates and the trailing required marker."""
    cleaned = _MERGE_FIELD.sub("", label)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned.rstrip("*").strip().rstrip(":").strip()


def is_placeholder_prose(text: str) -> bool:
    """True when an option's text is instructions rather than a value."""
    stripped = text.strip().lower()
    if not stripped:
        return True
    return any(stripped.startswith(p) for p in _PLACEHOLDER_PROSE)


def clean_options(options: list[Option] | None) -> tuple[list[Option] | None, bool]:
    """Drop placeholder prose from an option list and report whether the set is open.

    Returns `(options, openSet)`. `openSet` is True when the control offered
    choices but none of them were values -- the typeahead case, where the honest
    answer is that the set could not be enumerated, not that it has one member.
    """
    if options is None:
        return None, False
    real = [o for o in options if not is_placeholder_prose(o.label)]
    if not real:
        # Every choice was prose. The set exists but was not enumerable.
        return None, bool(options)
    return real, False


def merge_captures(
    required_a: bool,
    options_a: list[Option] | None,
    required_b: bool,
    options_b: list[Option] | None,
) -> tuple[bool, list[Option] | None]:
    """Combine two captures of one fact: OR the requiredness, keep the richer options."""
    required = required_a or required_b
    count_a = len(options_a) if options_a else 0
    count_b = len(options_b) if options_b else 0
    return required, (options_a if count_a >= count_b else options_b)


def catalog_type(
    portal_type: str,
    unit: str | None,
    option_count: int,
    canonical: str,
    label: str,
    open_set: bool,
) -> str:
    """Map a portal control to a `question_catalog` type. Arch doc 3.5, in order.

    `unit` beats the HTML type unless there are two or more options, because a
    unit describes the value and an option list describes the control, and a
    control offering choices is an enum whatever its values mean.
    """
    by_unit = {"usd": "currency", "date": "date", "percent": "number", "count": "number"}
    if unit in by_unit and option_count < 2:
        mapped = by_unit[unit]
    elif portal_type in ("toggle", "checkbox"):
        mapped = "boolean"
    elif portal_type == "number":
        mapped = "number"
    elif portal_type == "date":
        mapped = "date"
    elif portal_type == "tel":
        mapped = "phone"
    elif portal_type == "postal":
        mapped = "string"
    elif portal_type in ("select", "radio"):
        mapped = "enum"
    elif portal_type == "stepper":
        mapped = "enum" if option_count >= 2 else "number"
    elif portal_type == "other":
        mapped = "enum" if option_count else "string"
    else:
        mapped = "string"

    # The four post-rules, applied in the order the arch doc states them.
    if mapped == "phone" and _POSTAL_CANONICAL.search(canonical):
        mapped = "string"
    if mapped == "date" and _YEAR_QUESTION.search(label):
        mapped = "number"
    if mapped == "string" and option_count and not open_set:
        mapped = "enum"
    if open_set and mapped == "enum":
        mapped = "string"
    return mapped
