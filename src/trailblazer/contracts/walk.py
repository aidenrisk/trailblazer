"""Contracts between Frontier, FormFiller, Loop and ReplayGen.

The Scraper's page shape lives in `page_description`; everything that happens
*to* a page lives here: Frontier's memory, the one Assignment it issues,
FormFiller's report, the Diff the Scraper returns alongside a fresh page, and
the Walk that ReplayGen compiles.

Agents never call each other. Every model here crosses exactly one edge that
MASTER.md allows, and Loop is the thing carrying it.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Discriminator, Field

from trailblazer.contracts.page_description import ControlType, Option, RevealedBy


class ControlState(BaseModel):
    """
    Frontier's exploration record for ONE control on the form.

    Frontier tracks every control, not just the branching ones. It doesn't need
    to know in advance which controls branch: it walks them in page order, and a
    control turns out to be a chooser either because Scraper reported `options`
    or because FormFiller discovered them while filling it.

    Attributes:
    - fieldId / label / stageId / locator / type / required: copied from the Control
    - explored: True once this control is completely done (filled, or all its
                options walked). Frontier will not move past a control that is
                not explored.
    - options: None = we don't know whether this control has options yet.
               []   = confirmed plain field, no options.
               [..] = the choices, each with its own locator.
    - walked: options already tried, in the order tried. walked[-1] is what is
              currently set on the page.
    - pending: options still to try
    - revealedBy: the gate condition that makes this control exist, if any
    """

    fieldId: str
    label: str
    stageId: str
    locator: str
    type: ControlType
    required: bool = False
    explored: bool = False
    options: list[Option] | None = None
    walked: list[Option] = Field(default_factory=list)
    pending: list[Option] = Field(default_factory=list)
    revealedBy: RevealedBy | None = None


class FrontierBoard(BaseModel):
    """
    Frontier's memory: the state of the entire walk so far.

    Lives inside Frontier -- Loop does NOT pass this in or out. This model exists
    so the board can still be serialized for logging and debugging.
    """

    controls: list[ControlState] = Field(default_factory=list)
    currentStageId: str
    status: Literal[
        "exploring",
        "awaiting_fill",
        "slice_stable",
        "advancing",
        "backtracking",
        "complete",
        "blocked",
    ]


class SetOptionAssignment(BaseModel):
    """
    "Select this specific option of this control."
    Frontier -> FormFiller, once a control's options are known.

    `locator` is the OPTION's own locator whenever the option has one; FormFiller
    must act on it, NOT on `controlLocator`. `controlLocator` is the parent's
    locator, there so FormFiller can open a custom widget before picking, and
    what `locator` falls back to when the option carries none of its own.
    """

    type: Literal["set_option"] = "set_option"
    fieldId: str
    option: str
    locator: str
    controlLocator: str


class FillFieldAssignment(BaseModel):
    """
    "Fill this ONE control with this value." Frontier -> FormFiller, for a
    control with no known options.

    This is also how a hidden chooser gets discovered: FormFiller tries to fill
    it, finds it is a dropdown, and reports the options back on
    `FillReport.discoveredOptions`.
    """

    type: Literal["fill_field"] = "fill_field"
    fieldId: str
    locator: str
    value: str


class SimpleAssignment(BaseModel):
    """
    Basic navigation and control commands.
    - "next": click the Next button, advance to next page
    - "back": undo the last choice, click Back
    - "submit": submit the form (no Next button, this is the last action)
    - "stop": stop here (form complete, blocked, or error)
    """

    type: Literal["next", "back", "submit", "stop"]


class LastPageProbeAssignment(BaseModel):
    """(v1+) On the last page, probe whether optional fields matter."""

    type: Literal["last_page_optional_probe"] = "last_page_optional_probe"


def _assignment_discriminator(v: Union[dict, BaseModel]) -> str:
    if isinstance(v, dict):
        return v.get("type", "unknown")
    return v.type


Assignment = Annotated[
    Union[
        SetOptionAssignment,
        FillFieldAssignment,
        SimpleAssignment,
        LastPageProbeAssignment,
    ],
    Discriminator(_assignment_discriminator),
]


class FillStep(BaseModel):
    """One thing FormFiller actually did on the page while executing an Assignment."""

    fieldId: str | None = None
    action: Literal["fill", "select", "toggle", "click", "type"]
    locator: str
    value: str | None = None
    required: bool = False


ErrorClass = Literal["not_found", "not_unique", "widget", "validation"]
"""Why a fill failed."""


class FillReport(BaseModel):
    """
    FormFiller -> Loop: "here's what I did, and here's what I learned."

    The bottom three fields are how FormFiller talks to Frontier. Agents never
    call each other, so this travels via Loop.

    - discoveredOptions:
        None = "not a chooser" (or nothing new learned).
        []   = "I opened it and it genuinely has no options."
        [..] = "This control is actually a chooser, and these are its options."
    - chosenOption: the label FormFiller actually picked.
    """

    ok: bool
    steps: list[FillStep] = Field(default_factory=list)
    advance: bool = False
    landed: list[str] = Field(default_factory=list)
    errorClass: ErrorClass | None = None

    fieldId: str | None = None
    discoveredOptions: list[Option] | None = None
    chosenOption: str | None = None


class ChangedControl(BaseModel):
    """A minimal reference to a control that changed."""

    label: str
    locator: str
    fieldId: str | None = None


class Diff(BaseModel):
    """
    What changed on the page after FormFiller executed an Assignment.
    Scraper reports it alongside the fresh PageDescription.

    - "+ve": the page changed (revealed new fields, navigated, etc.)
    - "-ve": the page settled, nothing structural changed

    Advisory only. Frontier re-derives what is new by comparing the fresh
    PageDescription against its board.
    """

    polarity: Literal["+ve", "-ve"]
    addedControls: list[ChangedControl] = Field(default_factory=list)
    removedControls: list[ChangedControl] = Field(default_factory=list)
    changedControls: list[ChangedControl] = Field(default_factory=list)


class WalkStep(BaseModel):
    """
    One atomic action in a successful walk through the form.

    - value: the literal typed, when it came from neither applicant data nor credentials
    - canonical: applicant data field path, when filling with applicant data
    - credentialKey: which credential, when filling a login control
    - option: which option, when choosing
    """

    action: Literal["type", "choose", "toggle", "click", "wait-for", "back"]
    fieldId: str | None = None
    locator: str
    value: str | None = None
    canonical: str | None = None
    credentialKey: str | None = None
    option: str | None = None


WalkSlice = list[WalkStep]


class WalkPath(BaseModel):
    """
    One replayable path through the form, with the branch it represents.

    `choices` pins exactly one option per chooser on this path, so ReplayGen can
    name the Program it compiles and Validator can report which branch failed.
    """

    choices: dict[str, str] = Field(default_factory=dict)
    steps: WalkSlice = Field(default_factory=list)


class Walk(BaseModel):
    """
    Frontier -> ReplayGen: every path captured, one WalkPath per branch.

    Path count follows MASTER.md's "do not walk combinations of independent
    gates": a baseline path taking each chooser's first option, plus one variant
    per remaining option.
    """

    paths: list[WalkPath] = Field(default_factory=list)

    @property
    def slices(self) -> list[WalkSlice]:
        """Just the step sequences, for callers that don't care which branch."""
        return [p.steps for p in self.paths]
