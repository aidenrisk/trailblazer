"""What the vision fallback is asked and what it answers.

Field names are camelCase for the same reason the other contracts are: the wire
format is the contract (architecture spec, 3.1).
"""

from pydantic import BaseModel, ConfigDict, Field


class Anchor(BaseModel):
    """One badged element, identified by the words a person would read near it.

    The model never writes a selector. It reads the screenshot and reports the
    text it sees: the label beside or above the element, and the heading of the
    row, column or section it sits in. Python turns those words into candidate
    locators, measures them, and proves each one resolves back to the very
    element that carried the badge. So a wrong reading costs a rejected
    candidate, never a wrong element acted upon.
    """

    model_config = ConfigDict(populate_by_name=True)

    badge: int
    """The number drawn on the element in the screenshot."""

    label: str = ""
    """The words closest to the element, as a person reads them: the row label
    for a cell, the caption beside an input. Empty when nothing is legible."""

    heading: str = ""
    """The column header or section heading the element sits under, when the
    label alone would not distinguish it from its siblings."""

    purpose: str = ""
    """What the field is asking for, in the page's own words. Recorded as the
    control's label when the extractor could not read one."""


class VisionReading(BaseModel):
    """The model's answer for one screenshot.

    `relevant` names the badges the page's own message is about, which is what
    turns a table-level rejection -- belonging to no field -- into fields the
    filler can act on.
    """

    model_config = ConfigDict(populate_by_name=True)

    anchors: list[Anchor] = Field(default_factory=list)
    relevant: list[int] = Field(default_factory=list)
    """Badges the visible error or instruction refers to, most likely first.
    Empty when the page shows no such message."""

    note: str = ""
    """What the model saw that the caller could not, in one sentence. Logged,
    never acted on."""
