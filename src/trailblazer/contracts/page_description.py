"""The scraper's output contract: one page of a carrier form, described.

Field names are camelCase because the wire format *is* the contract (see
the architecture spec, §3.1). `populate_by_name` lets Python callers use those names
without an alias layer.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ControlType = Literal["text", "select", "toggle", "date", "number", "other"]

# Types whose "choices" are meaningless: they must carry `options: None`, not [].
_NO_OPTION_TYPES = {"text", "number", "date"}

# A Playwright accessibility-snapshot ref, e.g. "e12". Never a valid locator.
_SNAPSHOT_REF = re.compile(r"^e\d+$")


class Option(BaseModel):
    """One choice of a multiple-choice control, with its own address.

    A native `<select>` holds its choices as `<option>` nodes, which are not
    clickable: the control is set with `select_option(label)` against the
    select's own locator. A radio group has no such single node -- each choice
    is a separate input, and the only way to set the answer is to click one.
    So `locator` is populated exactly when the choice is its own addressable
    node, and `None` when the parent's locator plus `label` is the whole
    address. Downstream reads it as: locator present, click it; locator absent,
    select `label` on the parent.
    """

    model_config = ConfigDict(populate_by_name=True)

    label: str
    """The choice as a person reads it. This is what `select_option` takes."""

    locator: str | None
    """Playwright address of this choice, when it has one of its own.

    Measured by `_first_unique` at perceive time like every other locator, so
    it is a verified `count() == 1` claim and never a model's proposal.
    """

    @field_validator("locator")
    @classmethod
    def _reject_snapshot_ref(cls, v: str | None) -> str | None:
        """Same guard as `Control.locator`: a ref like `e12` dies on re-render."""
        if v is not None and _SNAPSHOT_REF.match(v):
            raise ValueError(f"locator {v!r} is an accessibility snapshot ref, not a locator")
        return v


class RevealedBy(BaseModel):
    """The assignment that made a control appear since the prior perceive."""

    model_config = ConfigDict(populate_by_name=True)

    fieldId: str
    equals: str


class Control(BaseModel):
    """One addressable input on the page."""

    model_config = ConfigDict(populate_by_name=True)

    fieldId: str
    """Per-page counter, `q_001`. Reset every perceive; not cross-page identity."""

    key: str = Field(exclude=True)
    """The extractor payload's per-element key (`el_0`), echoed back by the model.

    It exists so the measured `locator` and `unique` can be matched back onto the
    right control after the model returns. No default, so it lands in the JSON
    schema's `required` list: a model that drops it fails structured-output
    parsing loudly instead of leaving the join to guesswork. `exclude=True`
    keeps it out of the serialized output, which the architecture spec fixes at
    exactly nine fields.
    """

    label: str
    type: ControlType
    required: bool

    options: list[Option] | None
    """The choice list, never the chosen value. `None` when choices are not in the DOM.

    Each entry carries the choice's label and, where the choice is its own
    clickable node, its measured locator -- which is what makes a radio group
    expressible as one control instead of one control per choice.
    """

    locator: str
    """Playwright address. Never a snapshot ref."""

    unique: bool

    disabled: bool = False
    """Not interactable: `disabled`, `readonly`, or `aria-disabled`.

    Pie's "Agency / Program" is pre-filled from the logged-in agency and cannot
    be touched. Without this the field looks like an ordinary control, and an
    assignment against it spends the filler's click timeout before failing.
    """
    """Verified by `page.locator(locator).count() == 1`."""

    revealedBy: RevealedBy | None

    formatHint: str = Field(default="", exclude=True)
    """What the page itself says about the shape it wants: the placeholder, a
    `pattern`, a length or numeric bound, a `title`, the `aria-describedby` text.

    Measured by the extractor and restored after the model returns, like
    `locator`. It reaches the filler's value chooser, which otherwise learns a
    format only by having a value rejected -- a full page round trip and a
    second model call to discover something the DOM already stated.

    `exclude=True` keeps the serialized shape at the nine fields the spec fixes.
    """

    helpText: str = Field(default="", exclude=True)
    """What the field's help tooltip says, read by hovering its icon during the
    scrape.

    Pie states the FEIN rule -- "a unique 9-digit number" -- nowhere but a
    tooltip behind a bare 16px icon beside the label; the rejection message says
    only "Please enter the FEIN". So this is read on the first pass, before any
    attempt, and it is what lets the first fill be right. Measured and restored
    like `formatHint`, excluded from the serialized shape for the same reason.
    """

    @field_validator("locator")
    @classmethod
    def _reject_snapshot_ref(cls, v: str) -> str:
        """A ref like `e12` indexes into one snapshot and dies on re-render."""
        if _SNAPSHOT_REF.match(v):
            raise ValueError(f"locator {v!r} is an accessibility snapshot ref, not a locator")
        return v

    @model_validator(mode="after")
    def _options_none_for_scalar_types(self) -> "Control":
        """`text`/`number`/`date` carry no choices; `[]` would read as 'zero choices'."""
        if self.type in _NO_OPTION_TYPES and self.options is not None:
            raise ValueError(f"type {self.type!r} must have options=None, got {self.options!r}")
        return self


class Action(BaseModel):
    """A clickable element the page can be advanced by.

    Reported for pages that hold nothing fillable -- a dashboard, a
    business-type chooser -- where the only move is to click one specific thing.
    Frontier chooses which; the scraper never clicks.
    """

    model_config = ConfigDict(populate_by_name=True)

    label: str
    """The element's visible text."""

    href: str = ""
    """The link target, empty for a button. Frontier matches on it."""

    locator: str
    unique: bool


class PageDescription(BaseModel):
    """Everything the downstream pipeline needs to know about one form page."""

    model_config = ConfigDict(populate_by_name=True)

    stageId: str
    """`form_page_<index>_<slug>`. Index from Loop, slug derived from the page."""

    url: str
    controls: list[Control]

    next: str | None
    """Locator for the forward button, if there is one."""

    back: str | None

    actions: list[Action] = []
    """Clickable elements, for a page whose only move is to advance."""

    blockers: list[str]
    """Validation text, overlays, decline chrome."""

    @field_validator("next", "back")
    @classmethod
    def _reject_snapshot_ref(cls, v: str | None) -> str | None:
        """Same guard as `Control.locator`: `next`/`back` are locators too."""
        if v is not None and _SNAPSHOT_REF.match(v):
            raise ValueError(f"locator {v!r} is an accessibility snapshot ref, not a locator")
        return v

