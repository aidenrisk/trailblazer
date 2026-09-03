"""
Shared data contracts for all agents.

These models define the exact shapes of data passed between agents in the pipeline.
Think of them as contracts: if one agent sends data shaped like PageDescription,
the next agent knows exactly what fields to expect and can validate them.

All models use Pydantic, which automatically:
- Validates data when created (rejects bad types/missing fields)
- Can serialize to/from JSON cleanly
- Provides helpful error messages if data is wrong
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class RevealedBy(BaseModel):
    """
    Describes when a form field appears conditionally.
    E.g., "Members field only shows if Entity Type == LLC"

    - fieldId: which field controls this visibility
    - equals: what value that field must have for this field to appear
    """
    fieldId: str
    equals: str


class Option(BaseModel):
    """
    One choice for a select/toggle control, with its own locator to select it.

    A shared control locator isn't enough on its own: a native <select> needs a
    distinct locator per <option>, and a gate made of separate elements (e.g. a
    "Yes" button and a "No" button) needs a distinct locator per option too.
    Every Option is grounded in a real locator scraped/probed from the page —
    never invented.
    """
    label: str
    locator: str


class Control(BaseModel):
    """
    A single form field (input, dropdown, checkbox, etc.) on a page.

    Attributes:
    - fieldId: unique ID for this field (e.g., "q_001"). Used to reference it everywhere.
    - label: human-readable name (e.g., "Business Name")
    - type: what kind of input it is (text box, dropdown, toggle, etc.)
    - required: must this field be filled before moving forward?
    - options: if it's a chooser, the list of choices, each with its own
              locator (e.g., [{"label": "LLC", "locator": "..."}, ...]).
              None means UNKNOWN, not "no options" — many widgets (custom
              dropdowns, comboboxes) don't render their option list in the DOM
              until clicked open, so Scraper often can't report this up front.
              An empty list means "confirmed: this control has no options"
              (i.e. it's a plain field). FormFiller may discover options for a
              control Scraper reported as None; it reports them back on
              FillReport.discoveredOptions and Frontier walks them.
    - locator: Playwright address to find this field on the page (e.g., "#entityType").
              This is stable across page reloads, not a screenshot reference.
    - unique: is this locator guaranteed to match only one element on the page?
    - revealedBy: if not None, this field only appears if another field has a certain value
    """
    fieldId: str
    label: str
    type: Literal["text", "select", "toggle", "date", "number", "other"]
    required: bool
    options: list[Option] | None = None
    locator: str
    unique: bool
    revealedBy: RevealedBy | None = None


class PageDescription(BaseModel):
    """
    What Scraper sees and reports to Frontier.
    "Here's what's on the page right now: these fields, these buttons, etc."

    Attributes:
    - stageId: which application stage is this? (e.g., "form_page_1_business_info")
    - url: current page URL
    - controls: all form fields visible on this page
    - next: locator for the "Next" button (if present), or None if this is the last page
    - back: locator for "Back" button (if present)
    - candidateGates: fieldIds that look like they branch (select/toggle with options).
                      NOTE: no longer consumed. Frontier explores every control one
                      by one, so it doesn't need to be told in advance which ones
                      branch. Kept because it's legitimate Scraper output, but
                      nothing reads it — don't wire new logic to it.
    - blockers: validation errors, overlays, or decline messages on the page that prevent proceeding
    """
    stageId: str
    url: str
    controls: list[Control]
    next: str | None
    back: str | None
    candidateGates: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


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

    Walk strategy for a chooser:
      Discover:     options=[Male, Female], pending=[Male, Female], walked=[]
      Pick Female:  pending=[Male],         walked=[Female],        explored=False
      Pick Male:    pending=[],             walked=[Female, Male],  explored=True
    Only now may Frontier move to the next control.
    """

    fieldId: str
    label: str
    stageId: str
    locator: str
    type: Literal["text", "select", "toggle", "date", "number", "other"]
    required: bool = False
    explored: bool = False
    options: list[Option] | None = None
    walked: list[Option] = Field(default_factory=list)
    pending: list[Option] = Field(default_factory=list)
    revealedBy: RevealedBy | None = None
    """
    Copied from the Control. This is how an action gets attributed to a branch:
    a field that only exists when q_gender == "Female" belongs to the Female
    path and must be left out of the Male one.
    """


class FrontierBoard(BaseModel):
    """
    Frontier's memory: the state of the entire walk so far.

    Lives inside Frontier — Loop does NOT pass this in or out. Frontier is the
    only agent that needs it, so it owns it. This model exists so the board can
    still be serialized for logging/debugging.

    Attributes:
    - controls: exploration record for every control seen so far, in the order
                they were first seen (page order, with revealed controls appended)
    - currentStageId: which page are we on right now?
    - status: high-level state machine
        * "exploring": absorbing feedback, deciding what's next
        * "awaiting_fill": an assignment was issued, waiting for FormFiller
        * "slice_stable": a walk slice is ready to send to ReplayGen
        * "advancing": page fully explored, clicking Next
        * "backtracking": (v1) undoing a choice to try another option
        * "complete": entire form walked, walk slice published
        * "blocked": validation error or blocker prevents moving forward
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


class Assignment(BaseModel):
    """
    Frontier -> FormFiller: exactly ONE control, exactly ONE action.

    One flat model rather than a union, because every assignment answers the
    same four questions:

        {"type": ..., "fieldId": ..., "option": ..., "locator": ...}

        type     what to do
        fieldId  which control (None for navigation)
        option   which choice — an Option object, or None when this isn't a choice
        locator  the CONTROL's locator (None for navigation)

    The option carries its own locator, which is why there's no separate
    `controlLocator` field: `locator` is the parent control (open the widget
    with it), `option.locator` is the thing to actually click. A native <select>
    addresses each <option> separately, and a split control (paired Yes/Maybe
    buttons) has a distinct locator per button — so FormFiller acts on
    `option.locator`, falling back to `locator` when a discovered option carries
    no distinct locator of its own.

    The shapes, by type:

      fill_field   fieldId + locator, option=None
                   "Fill this control." FormFiller picks the literal value —
                   it's the one holding the element, so it's the one that knows
                   what the field will accept — and reports what it typed back
                   on FillReport.steps[].value, which is what the walk slice
                   records and ReplayGen compiles.
                   Doubles as discovery: FormFiller tries to fill it, finds it's
                   actually a dropdown, and reports what it found back on
                   FillReport.discoveredOptions. Frontier then walks the
                   remaining options before moving to the next control.

      set_option   fieldId + locator + option
                   "Select this option of this control." Issued once per option,
                   because Frontier walks a chooser one branch at a time.

      next | back | submit | stop        no fields
                   Navigation. "back" undoes the last choice; "stop" ends the
                   walk (complete, blocked, or errored).

      last_page_optional_probe           no fields
                   (v1+) we're on the last page — probe whether optional fields
                   matter.

    One control per assignment. Frontier never fills a whole page at once,
    because it has to see what each individual fill does to the page.
    """

    type: Literal[
        "fill_field",
        "set_option",
        "next",
        "back",
        "submit",
        "stop",
        "last_page_optional_probe",
    ]
    fieldId: str | None = None
    option: Option | None = None
    locator: str | None = None

    @property
    def targets_control(self) -> bool:
        """True for the two types that act on a control rather than navigate."""
        return self.type in ("fill_field", "set_option")

    @property
    def action_locator(self) -> str | None:
        """
        What FormFiller should actually click/fill.

        For set_option that's the option's own locator when it has one; for
        everything else it's the control's.
        """
        if self.type == "set_option" and self.option is not None:
            return self.option.locator or self.locator
        return self.locator

    @model_validator(mode="after")
    def _check_shape(self) -> "Assignment":
        if self.type == "set_option":
            if self.option is None:
                raise ValueError("set_option requires an option")
            if not self.fieldId or not self.locator:
                raise ValueError("set_option requires fieldId and locator")
        elif self.type == "fill_field":
            if self.option is not None:
                raise ValueError("fill_field must not carry an option")
            if not self.fieldId or not self.locator:
                raise ValueError("fill_field requires fieldId and locator")
        else:
            # Navigation and probe assignments address no control at all.
            if self.fieldId or self.option or self.locator:
                raise ValueError(f"{self.type} assignment takes no control fields")
        return self


class FillStep(BaseModel):
    """
    One thing FormFiller actually did on the page while executing an Assignment.

    Attributes:
    - fieldId: which control (None for navigation clicks)
    - action: what kind of interaction it performed
    - locator: the Playwright address it acted on
    - value: what it typed/selected, if anything
    - required: was the control required?
    """
    fieldId: str | None = None
    action: Literal["fill", "select", "toggle", "click", "type"]
    locator: str
    value: str | None = None
    required: bool = False


class FillReport(BaseModel):
    """
    FormFiller → Loop: "here's what I did, and here's what I learned."

    The bottom three fields are how FormFiller talks to Frontier. Agents never
    call each other, so this travels via Loop: Loop hands the FillReport to
    Frontier on its next call, and Frontier absorbs it.

    Attributes:
    - ok: did the assignment execute?
    - steps: the interactions performed
    - advance: did the page navigate as a result?
    - landed: fieldIds that actually took a value
    - errorClass: why it failed, if it failed

    Filler → Frontier feedback:
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
    ok: bool
    steps: list[FillStep] = Field(default_factory=list)
    advance: bool = False
    landed: list[str] = Field(default_factory=list)
    errorClass: Literal["not_found", "not_unique", "widget", "validation"] | None = None

    fieldId: str | None = None
    discoveredOptions: list[Option] | None = None
    chosenOption: str | None = None


class ChangedControl(BaseModel):
    """
    A minimal reference to a control that changed.
    (Not a full Control object, just enough to identify it.)
    """
    label: str
    locator: str
    fieldId: str | None = None


class Diff(BaseModel):
    """
    What changed on the page after FormFiller executed an Assignment.
    Scraper reports it alongside the fresh PageDescription.

    - "+ve" (positive diff): the page changed (revealed new fields, navigated, etc.)
    - "-ve" (negative diff): the page settled, nothing structural changed

    Advisory only. Frontier does NOT drive its queue from addedControls — it
    re-derives what's new by comparing the fresh PageDescription against the
    board, which is robust even if the diff is imprecise. The polarity is used
    for logging and to explain what just happened.

    Attributes:
    - polarity: did the page change?
    - addedControls: fields that appeared after the action
    - removedControls: fields that disappeared
    - changedControls: fields that existed but changed (e.g., options updated)
    """
    polarity: Literal["+ve", "-ve"]
    addedControls: list[ChangedControl] = Field(default_factory=list)
    removedControls: list[ChangedControl] = Field(default_factory=list)
    changedControls: list[ChangedControl] = Field(default_factory=list)


class WalkStep(BaseModel):
    """
    One atomic action in a successful walk through the form.
    E.g., "type 'Harbor Point Bistro LLC' into the Business Name field"
         "choose 'LLC' from the Entity Type dropdown"
         "click the Next button"

    These are ordered steps that ReplayGen will turn into a reusable script.

    Attributes:
    - action: what kind of action is this?
    - fieldId: which field (if applicable)?
    - locator: Playwright address to find the element
    - value: the literal value that was typed, if it came from neither applicant
             data nor credentials (today Frontier uses synthetic values, so this
             is what actually landed and what ReplayGen needs to compile)
    - canonical: if filling with applicant data, which field path? (e.g., "business.legal_name")
    - credentialKey: if using a secret, which credential? (e.g., "LOGIN_EMAIL")
    - option: if choosing an option, which one? (e.g., "LLC")
    """
    action: Literal["type", "choose", "toggle", "click", "wait-for", "back"]
    fieldId: str | None = None
    locator: str
    value: str | None = None  # literal value typed
    canonical: str | None = None  # applicant data field path
    credentialKey: str | None = None  # secret/credential key
    option: str | None = None  # for option choices


# WalkSlice is one replayable path's worth of ordered steps.
# Example: entity type = LLC produces a slice with:
#   [WalkStep(action="choose", option="LLC", locator="#entityType", ...),
#    WalkStep(action="type", canonical="business.legal_name", locator="#businessName", ...),
#    WalkStep(action="click", locator="button:has-text('Next')", ...)]
WalkSlice = list[WalkStep]


class WalkPath(BaseModel):
    """
    One replayable path through the form, with the branch it represents.

    `choices` pins exactly one option per chooser on this path, so ReplayGen can
    name the Program it compiles and Validator can report which branch failed.

    Attributes:
    - choices: fieldId -> option label held fixed on this path
    - steps: the ordered actions for this path, and only this path
    """

    choices: dict[str, str] = Field(default_factory=dict)
    steps: WalkSlice = Field(default_factory=list)


class Walk(BaseModel):
    """
    Frontier -> ReplayGen: every path captured, one WalkPath per branch.

    NOT one slice per walk. A form with a two-option chooser has two distinct
    paths through it, and each needs its own Program — a single slice containing
    both options would replay as "click Male, click Female", which ends on
    Female and never exercises Male at all.

    Path count follows MASTER.md's "do not walk combinations of independent
    gates": a baseline path taking each chooser's first option, plus one variant
    per remaining option. Three choosers with 2/3/2 options give
    1 + 1 + 2 + 1 = 5 paths, not 2 x 3 x 2 = 12.
    """

    paths: list[WalkPath] = Field(default_factory=list)

    @property
    def slices(self) -> list[WalkSlice]:
        """Just the step sequences, for callers that don't care which branch."""
        return [p.steps for p in self.paths]

__all__ = [
    # Scraper output
    "RevealedBy",
    "Option",
    "Control",
    "PageDescription",
    # Frontier memory
    "ControlState",
    "FrontierBoard",
    # Frontier -> FormFiller
    "Assignment",
    # FormFiller -> Loop -> Frontier
    "FillStep",
    "FillReport",
    # Scraper -> Frontier
    "ChangedControl",
    "Diff",
    # Frontier -> ReplayGen
    "WalkStep",
    "WalkSlice",
    "WalkPath",
    "Walk",
]
