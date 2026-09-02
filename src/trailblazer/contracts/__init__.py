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

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Discriminator, Field


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


class SetOptionAssignment(BaseModel):
    """
    "Select this specific option of this control."
    Frontier → FormFiller, once a control's options are known.

    IMPORTANT — which locator to use:
      `locator` is the OPTION's own locator whenever the option has one. A native
      <select> gives each <option> a distinct locator, and a split control (paired
      "Yes" / "Maybe" buttons) gives each button its own locator. In both cases
      FormFiller must act on `locator`, NOT on `controlLocator`.

      `controlLocator` is the parent control's locator. It's there so FormFiller
      can open a custom widget before picking, and it's what `locator` falls back
      to when the discovered options carry no distinct locator of their own.
    """
    type: Literal["set_option"] = "set_option"
    fieldId: str  # which control's option are we setting?
    option: str  # the option's label (e.g., "LLC")
    locator: str  # the OPTION's locator when it has one, else == controlLocator
    controlLocator: str  # the parent control's locator


class FillFieldAssignment(BaseModel):
    """
    "Fill this ONE control with this value."
    Frontier → FormFiller, for a control with no known options.

    This is also how a hidden chooser gets discovered: FormFiller tries to fill
    it, finds it's actually a dropdown, and reports the options it found back on
    FillReport.discoveredOptions. Frontier then walks the remaining options
    before moving on to the next control.

    One control per assignment — Frontier never fills a whole page at once,
    because it has to see what each individual fill does to the page.
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
    """
    "We're on the last page. Try to probe if optional fields matter."
    (v1+: advanced feature for exploring optional fields at form end)
    """
    type: Literal["last_page_optional_probe"] = "last_page_optional_probe"


# Assignment is a "discriminated union": one of several types, picked based on the "type" field.
# This lets Pydantic automatically parse JSON and create the right class.
# E.g., {"type": "set_option", "fieldId": "...", ...} → SetOptionAssignment
#       {"type": "next"} → SimpleAssignment
# Without this, you'd have to manually check the type and cast. This is cleaner.
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
    "SetOptionAssignment",
    "FillFieldAssignment",
    "SimpleAssignment",
    "LastPageProbeAssignment",
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
