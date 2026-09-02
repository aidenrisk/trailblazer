"""Frontier -> FormFiller: one thing to do, for one control.

Frontier never fills a whole page at once, because it has to see what each
individual action does to the page.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Discriminator


class SetOptionAssignment(BaseModel):
    """Select one option of a control whose choices are known.

    Which locator to act on — this mirrors `Option.locator` exactly:

    - `locator` is set: the choice is its own addressable node (a radio group's
      inputs, say). **Click it.**
    - `locator` is None: the choice has no node of its own, and the parent's
      locator plus the label is the whole address (a native `<select>`, whose
      `<option>` nodes are not clickable). **`select_option(option)` against
      `controlLocator`.**

    Frontier does not collapse the None away, because the two cases need
    different Playwright calls and only the option itself knows which applies.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["set_option"] = "set_option"
    fieldId: str
    option: str
    """The choice's label. This is also what `select_option` takes."""

    locator: str | None
    """The option's own locator, or None when it has no addressable node."""

    controlLocator: str
    """The parent control — how to open a custom widget, and the `select_option` target."""


class FillFieldAssignment(BaseModel):
    """Fill this ONE control with this value.

    Also how a hidden chooser gets discovered: FormFiller tries to fill it,
    finds it is actually a dropdown, and reports the options it found back on
    `FillReport.discoveredOptions`. Frontier then walks the remaining options
    before moving on.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["fill_field"] = "fill_field"
    fieldId: str
    locator: str
    value: str


class SimpleAssignment(BaseModel):
    """Navigation and control commands.

    - "next": click Next, advance to the following page
    - "back": undo the last choice, click Back
    - "submit": submit the form
    - "stop": stop here (blocked, or nothing further to do)
    """

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["next", "back", "submit", "stop"]


class LastPageProbeAssignment(BaseModel):
    """We are on the last page; probe whether optional fields matter. (v1+)"""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["last_page_optional_probe"] = "last_page_optional_probe"


def _assignment_discriminator(v: Union[dict, BaseModel]) -> str:
    if isinstance(v, dict):
        return v.get("type", "unknown")
    return v.type


# A discriminated union: one of several types, picked on the "type" field, so
# pydantic parses JSON into the right class without manual checks.
Assignment = Annotated[
    Union[
        SetOptionAssignment,
        FillFieldAssignment,
        SimpleAssignment,
        LastPageProbeAssignment,
    ],
    Discriminator(_assignment_discriminator),
]
