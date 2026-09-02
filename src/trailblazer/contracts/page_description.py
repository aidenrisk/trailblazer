"""The scraper's output contract: one page of a carrier form, described.

Field names are camelCase because the wire format *is* the contract (see
`scraper_io.txt`). `populate_by_name` lets Python callers use the same names
without an alias layer.

This module is the merge of two lines of work: the Scraper's validated shape
(measured locators, snapshot-ref guard, scalar types carry no options) and the
Frontier's construction ergonomics (defaults for the fields a hand-built page
rarely sets). Where the two disagreed, validation won and defaults were added.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ControlType = Literal["text", "select", "toggle", "date", "number", "other"]

CredentialKind = Literal["username", "password", "otp"]
"""What a login control asks for. Measured by the extractor, never by the model.

`username` covers email and user-id fields alike; `otp` is a one-time code
input, single or per-digit. A control with a kind is filled from credentials by
FormFiller and is never given a synthetic value or explored as a chooser.
"""

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

    locator: str | None = None
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

    key: str | None = Field(default=None, exclude=True)
    """The extractor payload's per-element key (`el_0`), echoed back by the model.

    It exists so the measured `locator` and `unique` can be matched back onto the
    right control after the model returns. Transport only: `exclude=True` keeps
    it out of the serialized output, and it is optional here so a page built by
    hand (fixtures, stubs, Frontier tests) need not invent one. The Scraper's
    own response schema re-declares it required, so a model that drops it still
    fails structured-output parsing loudly.
    """

    label: str
    type: ControlType
    required: bool

    options: list[Option] | None = None
    """The choice list, never the chosen value. `None` when choices are not in the DOM.

    `None` means UNKNOWN, not "no options": many widgets do not render their
    list until opened, so FormFiller may discover options for a control the
    Scraper reported as `None` and report them back. An empty list means
    "confirmed: no options".
    """

    locator: str
    """Playwright address. Never a snapshot ref."""

    unique: bool
    """Verified by `page.locator(locator).count() == 1`."""

    revealedBy: RevealedBy | None = None

    credential: CredentialKind | None = None
    """Set when this control is a login credential. Measured, never inferred by the model."""

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


class PageDescription(BaseModel):
    """Everything the downstream pipeline needs to know about one form page."""

    model_config = ConfigDict(populate_by_name=True)

    stageId: str
    """`form_page_<index>_<slug>`, or `login_<slug>` for a page carrying a credential control."""

    url: str
    controls: list[Control]

    next: str | None = None
    """Locator for the forward button, if there is one."""

    back: str | None = None

    candidateGates: list[str] = Field(default_factory=list)
    """fieldIds that may branch: every control with a non-empty `options` list.

    Legitimate Scraper output, but nothing routes on it: Frontier explores every
    control one by one, so it never needs to be told in advance which ones
    branch. Do not wire new logic to it.
    """

    blockers: list[str] = Field(default_factory=list)
    """Validation text, overlays, decline chrome."""

    @field_validator("next", "back")
    @classmethod
    def _reject_snapshot_ref(cls, v: str | None) -> str | None:
        """Same guard as `Control.locator`: `next`/`back` are locators too."""
        if v is not None and _SNAPSHOT_REF.match(v):
            raise ValueError(f"locator {v!r} is an accessibility snapshot ref, not a locator")
        return v

    @model_validator(mode="after")
    def _gates_reference_known_controls(self) -> "PageDescription":
        """A gate naming a fieldId that is not on the page would misroute Frontier."""
        known = {c.fieldId for c in self.controls}
        unknown = [g for g in self.candidateGates if g not in known]
        if unknown:
            raise ValueError(f"candidateGates reference unknown fieldIds: {unknown}")
        return self

    @property
    def is_login_stage(self) -> bool:
        """True when this page is part of the login, by the stage-name convention."""
        return self.stageId.startswith("login_")
