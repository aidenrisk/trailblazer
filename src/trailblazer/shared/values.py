"""
Picking a literal value to type into a plain field.

Lives in shared/ because FormFiller is what needs it — Frontier's Assignment
carries no value, so the agent holding the element is the one that decides what
to type. Shared rather than private to FormFiller so a real Playwright filler,
the stub, and the fixtures can all use the same rules.
"""

from typing import Callable

from trailblazer.contracts import Control, ControlState

ValueProvider = Callable[[Control | ControlState], str]


def synthetic_value(control: Control | ControlState) -> str:
    """
    Pick a value to type into a plain field.

    Type-aware so the fill actually lands: a date picker rejects "Test Value",
    a number field rejects it too, and an email field usually validates format.
    Label-sniffing is a heuristic, deliberately — this is capture-time
    exploration, not real applicant data. Real values arrive later, when the
    canonical/applicant mapping exists (see MASTER.md's `canonical` field on
    WalkStep); this function is the injectable seam where that plugs in.
    """
    label = control.label.lower()

    if control.type == "date":
        return "01/01/2026"
    if control.type == "number":
        if "premium" in label:
            return "10000"
        return "1000"

    # text/select/toggle/other: sniff the label for a format the field will accept
    if "email" in label:
        return "test@example.com"
    if "zip" in label or "postal" in label:
        return "10001"
    if "phone" in label:
        return "5551234567"
    if "fein" in label or "ein" in label or "tax id" in label:
        return "12-3456789"
    return "Test Value"
