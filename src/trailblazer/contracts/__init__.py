from typing import Annotated, Literal, Union

from pydantic import BaseModel, Discriminator, Field


class RevealedBy(BaseModel):
    fieldId: str
    equals: str


class Control(BaseModel):
    fieldId: str
    label: str
    type: Literal["text", "select", "toggle", "date", "number", "other"]
    required: bool
    options: list[str] = Field(default_factory=list)
    locator: str
    unique: bool
    revealedBy: RevealedBy | None = None


class PageDescription(BaseModel):
    stageId: str
    url: str
    controls: list[Control]
    next: str | None
    back: str | None
    candidateGates: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class Gate(BaseModel):
    gateId: str
    fieldId: str
    stageId: str
    kind: Literal["same-page", "last-page"]
    options: list[str]
    walked: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)


class FrontierBoard(BaseModel):
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
    type: Literal["set_option"] = "set_option"
    gateId: str
    option: str
    locator: str


class FillPageAssignment(BaseModel):
    type: Literal["fill_page"] = "fill_page"
    applicantSlice: dict[str, str]


class FillRevealedAssignment(BaseModel):
    type: Literal["fill_revealed"] = "fill_revealed"
    fieldId: str
    locator: str
    value: str


class SimpleAssignment(BaseModel):
    type: Literal["next", "back", "submit", "stop"]


class LastPageProbeAssignment(BaseModel):
    type: Literal["last_page_optional_probe"] = "last_page_optional_probe"


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
    label: str
    locator: str


class Diff(BaseModel):
    polarity: Literal["+ve", "-ve"]
    addedControls: list[ChangedControl] = Field(default_factory=list)
    removedControls: list[ChangedControl] = Field(default_factory=list)
    changedControls: list[ChangedControl] = Field(default_factory=list)


class WalkStep(BaseModel):
    action: Literal["type", "choose", "toggle", "click", "wait-for", "back"]
    fieldId: str | None = None
    locator: str
    canonical: str | None = None
    credentialKey: str | None = None
    option: str | None = None


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
