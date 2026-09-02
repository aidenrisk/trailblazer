"""Frontier -> ReplayGen: every path captured, one per branch."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WalkStep(BaseModel):
    """One atomic action in a successful walk.

    "type 'Harbor Point Bistro LLC' into Business Name", "choose 'LLC' from
    Entity Type", "click Next". Ordered steps that ReplayGen turns into a
    reusable script.

    Attributes:
    - action: what kind of action
    - fieldId: which control, if applicable
    - locator: Playwright address to act on
    - value: the literal value typed, when it came from neither applicant data
             nor credentials (today Frontier uses synthetic values, so this is
             what actually landed and what ReplayGen needs to compile)
    - canonical: applicant data path, e.g. "business.legal_name"
    - credentialKey: secret key, e.g. "LOGIN_EMAIL"
    - option: the choice's label, if this was a choice
    """

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["type", "choose", "toggle", "click", "wait-for", "back"]
    fieldId: str | None = None
    locator: str
    value: str | None = None
    canonical: str | None = None
    credentialKey: str | None = None
    option: str | None = None


# One replayable path's worth of ordered steps.
WalkSlice = list[WalkStep]


class WalkPath(BaseModel):
    """One replayable path through the form, with the branch it represents.

    Attributes:
    - choices: fieldId -> option label this path actually chooses. Derived from
               the steps, so it never claims a choice the path does not make.
    - steps: the ordered actions for this path, and only this path
    """

    model_config = ConfigDict(populate_by_name=True)

    choices: dict[str, str] = Field(default_factory=dict)
    steps: WalkSlice = Field(default_factory=list)


class Walk(BaseModel):
    """Every path captured, one WalkPath per branch.

    NOT one slice per walk. A form with a two-option chooser has two distinct
    paths through it, and each needs its own Program — a single slice containing
    both options would replay as "click Male, click Female", which ends on
    Female and never exercises Male at all.

    Path count follows MASTER.md's "do not walk combinations of independent
    gates": a baseline path taking each chooser's first option, plus one variant
    per remaining option. Choosers with 2/3/2 options give 1 + 1 + 2 + 1 = 5
    paths, not 2 x 3 x 2 = 12.
    """

    model_config = ConfigDict(populate_by_name=True)

    paths: list[WalkPath] = Field(default_factory=list)

    @property
    def slices(self) -> list[WalkSlice]:
        """Just the step sequences, for callers that do not care which branch."""
        return [p.steps for p in self.paths]
