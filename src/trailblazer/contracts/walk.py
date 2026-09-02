"""Contracts between Frontier, FormFiller, Loop and ReplayGen.

The Scraper's page shape lives in `page_description`; everything that happens
*to* a page lives here: Frontier's memory, the one Assignment it issues,
FormFiller's report, the Diff the Scraper returns alongside a fresh page, and
the Walk that ReplayGen compiles.

Agents never call each other. Every model here crosses exactly one edge that
MASTER.md allows, and Loop is the thing carrying it.

For the owners of ReplayGen and Validator, which this work does not build:

- A `WalkStep` with `credentialKey` compiles to IR `{ "action": "type",
  "valueFrom": "credential", "credentialKey": "LOGIN_EMAIL" }`, never to a
  literal value. `Walk.login` is the prefix a Program runs before any path.
- At run time, `LOGIN_EMAIL` and `LOGIN_PASSWORD` resolve through
  `trailblazer.agents.browser.login_actions.fill_credential` and `LOGIN_OTP`
  through `login_actions.clear_otp` (which pulls the code from the inbox, types
  it, and submits). Hold `trailblazer.agents.browser.login_lock.LoginLock` around
  the prefix for a carrier whose MFA is on.
- Verdicts: a recorded selector that no longer resolves is `defect` (portal
  drift; degrade the artifact). Steps that run but leave the tab on the login
  surface are `stuck` with reason `auth` (credentials; keep the artifact). A code
  that never clears is `stuck` with reason `mfa_timeout`.
  `trailblazer.agents.browser.login_replay` is the reference implementation for
  the login prefix and maps the same way.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Discriminator, Field, model_validator

from trailblazer.contracts.page_description import ControlType, Option, RevealedBy

# Credential keys a login fill may carry. The secret they name is resolved by
# FormFiller at capture time and by Validator's runner at replay time -- never
# by Frontier, which only ever sees the key.
LOGIN_EMAIL = "LOGIN_EMAIL"
LOGIN_PASSWORD = "LOGIN_PASSWORD"
LOGIN_OTP = "LOGIN_OTP"
CREDENTIAL_KEYS = frozenset({LOGIN_EMAIL, LOGIN_PASSWORD, LOGIN_OTP})


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
    - credential: copied from the Control; a credential control is filled from
                  credentials, never given a synthetic value or walked
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
    credential: Literal["username", "password", "otp"] | None = None


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
    "Fill this ONE control." Frontier -> FormFiller, for a control with no known options.

    Exactly one of `value` and `credentialKey` is set. A plain field gets a
    literal `value`. A credential control gets a `credentialKey` (`LOGIN_EMAIL`,
    `LOGIN_PASSWORD`, `LOGIN_OTP`) and FormFiller resolves it: the secret never
    appears in an Assignment, a FillReport, or a WalkStep. `LOGIN_OTP` is
    resolved by pulling the code from the inbox and clearing the challenge.

    This is also how a hidden chooser gets discovered: FormFiller tries to fill
    it, finds it is a dropdown, and reports the options back on
    `FillReport.discoveredOptions`.
    """

    type: Literal["fill_field"] = "fill_field"
    fieldId: str
    locator: str
    value: str | None = None
    credentialKey: str | None = None

    @model_validator(mode="after")
    def _value_xor_credential(self) -> "FillFieldAssignment":
        """A fill either types a value or resolves a credential, never both, never neither."""
        if (self.value is None) == (self.credentialKey is None):
            raise ValueError("fill_field needs exactly one of value or credentialKey")
        if self.credentialKey is not None and self.credentialKey not in CREDENTIAL_KEYS:
            raise ValueError(
                f"unknown credentialKey {self.credentialKey!r}; expected one of {sorted(CREDENTIAL_KEYS)}"
            )
        return self


class SimpleAssignment(BaseModel):
    """
    Basic navigation and control commands.
    - "next": click the Next button, advance to next page
    - "back": undo the last choice, click Back
    - "submit": submit the form (no Next button, this is the last action)
    - "stop": stop here (form complete, blocked, or error)

    `reason` says why a stop happened, in the same vocabulary as
    `FillReport.errorClass`: `auth` when the portal rejected the login (the
    login page came back unchanged after Next), `blocked` when the page carries
    a blocker. Loop reports it; nothing routes on it.
    """

    type: Literal["next", "back", "submit", "stop"]
    reason: Literal["auth", "blocked"] | None = None
    locator: str | None = None
    """The control to click for next/back/submit, from the page's own `next`/`back`.

    MASTER.md: an Assignment includes its locator. Frontier copies it from the
    PageDescription so FormFiller never has to re-derive which button is Next.
    """


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


ErrorClass = Literal["not_found", "not_unique", "widget", "validation", "auth", "mfa_timeout"]
"""Why a fill failed.

`auth` and `mfa_timeout` are the login failures: the portal rejected the
credentials, or the one-time code never cleared in the allowed window. Both are
distinct from `validation` so Loop can report them as needs-attention rather
than as form drift.
"""


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
    - credentialKey: which credential, when filling a login control. The step
                     never carries the secret itself.
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

    `login` is the prefix captured on `login_*` stages: the unconditional steps
    that get the tab authenticated, split out so they can be published per
    carrier while the form paths stay per (line, business type). The paths do
    NOT repeat them; a replay runs `login` first, then one path. Empty when the
    walk started from an already-authenticated tab.
    """

    login: WalkSlice = Field(default_factory=list)
    paths: list[WalkPath] = Field(default_factory=list)

    @property
    def slices(self) -> list[WalkSlice]:
        """Just the step sequences, for callers that don't care which branch."""
        return [p.steps for p in self.paths]
