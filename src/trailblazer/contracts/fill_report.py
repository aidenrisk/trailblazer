"""FormFiller -> Loop: what it did, and what it learned.

The bottom three fields of `FillReport` are how FormFiller talks to Frontier.
Agents never call each other, so this travels via Loop: Loop hands the report to
Frontier on its next call, and Frontier absorbs it.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trailblazer.contracts.page_description import Option


class FillStep(BaseModel):
    """One thing FormFiller actually did while executing an Assignment."""

    model_config = ConfigDict(populate_by_name=True)

    fieldId: str | None = None
    action: Literal["fill", "select", "toggle", "click", "type"]
    locator: str
    value: str | None = None
    required: bool = False


class FillReport(BaseModel):
    """Attributes:

    - ok: did the assignment execute?
    - steps: the interactions performed
    - advance: did the page navigate as a result?
    - landed: fieldIds that actually took a value
    - errorClass: why it failed, if it failed

    Filler -> Frontier feedback:
    - fieldId: which control this report concerns
    - discoveredOptions:
        None = "not a chooser" (or nothing new learned) — leave Frontier's
               knowledge of this control alone.
        []   = "I opened it and it genuinely has no options."
        [..] = "This control I was asked to fill is actually a chooser, and
               these are its options." Frontier must then walk them all before
               moving to the next control.
      The None-vs-[] distinction matters: a chooser with zero options would
      otherwise be asked to reveal its options forever and block the page.
    - chosenOption: the label FormFiller actually picked, so Frontier can mark
                    that one walked and not repeat it.
    """

    model_config = ConfigDict(populate_by_name=True)

    ok: bool
    steps: list[FillStep] = Field(default_factory=list)
    advance: bool = False
    landed: list[str] = Field(default_factory=list)
    errorClass: Literal["not_found", "not_unique", "widget", "validation"] | None = None

    fieldId: str | None = None
    discoveredOptions: list[Option] | None = None
    chosenOption: str | None = None
