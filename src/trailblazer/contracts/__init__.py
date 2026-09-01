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


class Control(BaseModel):
    """
    A single form field (input, dropdown, checkbox, etc.) on a page.

    Attributes:
    - fieldId: unique ID for this field (e.g., "q_001"). Used to reference it everywhere.
    - label: human-readable name (e.g., "Business Name")
    - type: what kind of input it is (text box, dropdown, toggle, etc.)
    - required: must this field be filled before moving forward?
    - options: if it's a select/toggle, the list of choices (e.g., ["LLC", "Corp"])
    - locator: Playwright address to find this field on the page (e.g., "#entityType").
              This is stable across page reloads, not a screenshot reference.
    - unique: is this locator guaranteed to match only one element on the page?
    - revealedBy: if not None, this field only appears if another field has a certain value
    """
    fieldId: str
    label: str
    type: Literal["text", "select", "toggle", "date", "number", "other"]
    required: bool
    options: list[str] = Field(default_factory=list)
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
                      These are hints to Frontier: "you might want to walk all options of these"
    - blockers: validation errors, overlays, or decline messages on the page that prevent proceeding
    """
    stageId: str
    url: str
    controls: list[Control]
    next: str | None
    back: str | None
    candidateGates: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class Gate(BaseModel):
    """
    A branching point that Frontier is tracking.
    "This form has a choice (Entity Type: LLC or Corporation) we need to explore."

    A gate is a select/toggle field with 2+ options that Frontier decides to walk all paths for.
    Frontier's job: try each option, see how the form changes, record what happened.

    Attributes:
    - gateId: unique ID for this gate (e.g., "g_entity_type")
    - fieldId: which Control is this gate based on?
    - stageId: which page did this gate appear on?
    - kind: "same-page" if clicking this option keeps us on the same page (reveals/hides fields),
            "last-page" if this is the final page (choices just change the walk, don't navigate)
    - options: all possible values for this gate (e.g., ["LLC", "Corporation"])
    - walked: which options have we already tried? (e.g., ["LLC"])
    - pending: which options still need trying? (e.g., ["Corporation"])

    Walk strategy:
      Start: pending=[all options], walked=[]
      Click option 1: FormFiller sets it, Frontier sees page change (+ve diff)
      Move option 1: pending=[opt2, opt3], walked=[opt1]
      Click option 2: FormFiller sets it
      ... continue until pending is empty
    """
    
    gateId: str
    fieldId: str
    stageId: str
    kind: Literal["same-page", "last-page"]
    options: list[str]
    walked: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)


class FrontierBoard(BaseModel):
    """
    Frontier's memory: the state of the entire walk so far.
    Persisted and updated after every PageDescription and Diff.

    Attributes:
    - gates: all gates discovered so far and their walked/pending progress
    - currentStageId: which page are we on right now?
    - status: high-level state machine
        * "exploring": actively finding/trying gates
        * "awaiting_fill": last assignment was issued, waiting for FormFiller to execute
        * "slice_stable": a walk slice is ready to send to ReplayGen
        * "advancing": ready to click Next to move to next page
        * "backtracking": (v1) undoing a choice to try another option
        * "complete": entire form filled successfully
        * "blocked": validation error or blocker prevents moving forward
    """
    gates: list[Gate] = Field(default_factory=list)
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
    "Click one option of a gate to see how the form reacts."
    Frontier → FormFiller, when exploring a branching choice.
    """
    type: Literal["set_option"] = "set_option"
    gateId: str  # which gate's option are we setting?
    option: str  # the specific value to select (e.g., "LLC")
    locator: str  # where to click/select it


class FillPageAssignment(BaseModel):
    """
    "Fill in all the required fields on this page."
    Frontier → FormFiller, when no gates are left to explore, just fill blanks.
    """
    type: Literal["fill_page"] = "fill_page"
    applicantSlice: dict[str, str]  # fieldId → value mapping (e.g., {"q_001": "Business Name Inc"})


class FillRevealedAssignment(BaseModel):
    """
    "Fill a field that was just revealed by a gate choice."
    (v1+: for now we don't use this much in v0)
    """
    type: Literal["fill_revealed"] = "fill_revealed"
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
# E.g., {"type": "set_option", "gateId": "...", ...} → SetOptionAssignment
#       {"type": "next"} → SimpleAssignment
# Without this, you'd have to manually check the type and cast. This is cleaner.
def _assignment_discriminator(v: Union[dict, BaseModel]) -> str:
    if isinstance(v, dict):
        return v.get("type", "unknown")
    return v.type


Assignment = Annotated[
    Union[
        SetOptionAssignment,
        FillPageAssignment,
        FillRevealedAssignment,
        SimpleAssignment,
        LastPageProbeAssignment,
    ],
    Discriminator(_assignment_discriminator),
]


class ChangedControl(BaseModel):
    """
    A minimal reference to a control that changed.
    (Not a full Control object, just enough to identify it.)
    """
    label: str
    locator: str


class Diff(BaseModel):
    """
    What changed on the page after FormFiller executed an Assignment.
    Loop compares PageDescription before and after, produces this.

    This is the signal Frontier uses to decide what to do next:
    - "+ve" (positive diff): page changed after the click (revealed new fields, etc.)
             → keep going, the action had an effect
    - "-ve" (negative diff): page didn't change (or settled)
             → this option's walk is complete, time to finalize and try next option

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
    - canonical: if filling with applicant data, which field path? (e.g., "business.legal_name")
    - credentialKey: if using a secret, which credential? (e.g., "LOGIN_EMAIL")
    - option: if choosing from a gate, which option? (e.g., "LLC")
    """
    action: Literal["type", "choose", "toggle", "click", "wait-for", "back"]
    fieldId: str | None = None
    locator: str
    canonical: str | None = None  # applicant data field path
    credentialKey: str | None = None  # secret/credential key
    option: str | None = None  # for gate choices


# WalkSlice is just a list of steps. Each successful walk creates one.
# Example: filling entity type = LLC creates a slice with:
#   [WalkStep(action="choose", option="LLC", locator="#entityType", ...),
#    WalkStep(action="type", canonical="business.legal_name", locator="#businessName", ...),
#    WalkStep(action="click", locator="button:has-text('Next')", ...)]
WalkSlice = list[WalkStep]

__all__ = [
    "RevealedBy",
    "Control",
    "PageDescription",
    "Gate",
    "FrontierBoard",
    "SetOptionAssignment",
    "FillPageAssignment",
    "FillRevealedAssignment",
    "SimpleAssignment",
    "LastPageProbeAssignment",
    "Assignment",
    "ChangedControl",
    "Diff",
    "WalkStep",
    "WalkSlice",
]
