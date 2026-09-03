"""The questions and metadata document shapes -- the external boundary.

Arch doc 3.3 and 3.4 fix these. They are read by the chat, the replay runner and
the self-heal agent, none of which are in this repo, so a field added or renamed
here is a break nothing local would catch.

The arch doc's layout (section 7) places these in `contracts/artifacts.py`. They
live here because this branch may not modify `contracts/`; the module is a
schema only, with no generator logic, so moving it is a file move.

The split is the whole point of the pair: metadata carries technical keys only
-- `questionId` and selectors -- and label, type, required, option text,
`exampleValue` and `conditional` live in questions and are not duplicated.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CatalogType = Literal[
    "text", "email", "tel", "number", "date", "textarea", "select", "radio",
    "checkbox", "toggle", "stepper", "file", "other", "string", "enum",
    "boolean", "currency", "phone",
]
"""The questions artifact's finer vocabulary (3.3) plus the catalog types 3.5
maps onto. Both appear because `type` carries the mapped value and the projection
reads it directly."""

Unit = Literal["date", "years", "months", "usd", "sqft", "count", "percent", "text"]

Outcome = Literal["quote", "appetite-decline", "stuck"]
"""Closed vocabulary. The runner derives success from it; a sixth value is unhandled."""


class Conditional(BaseModel):
    """The parent gate and the value that reveals this question."""

    model_config = ConfigDict(populate_by_name=True)

    questionId: str
    value: str


class Question(BaseModel):
    """One entry of the questions artifact. `carrier_manifest.doc`."""

    model_config = ConfigDict(populate_by_name=True)

    questionId: str
    """`q_001` onward, page order, stable within the flow. The join key."""

    page: str
    """The stage this question was captured on."""

    canonical: str
    """snake_case fact name, reused across carriers."""

    label: str
    """The exact on-screen question, merge fields stripped."""

    type: CatalogType
    required: bool

    options: list[str] | None = None
    """A closed set of values, else null. Never prose."""

    openSet: bool | None = None
    """True when a type-to-search list cannot be enumerated. Omitted when false."""

    exampleValue: str | None = None
    """The value actually entered during the walk. Without it a flow cannot
    self-validate after persist."""

    answerHint: str | None = None
    unit: Unit | None = None
    format: str | None = None
    """A literal pattern, e.g. `MM/DD/YYYY`."""

    derivesFrom: str | None = None
    satisfies: list[str] | None = None
    """Other canonical keys this one answer fills. Omitting it makes the chat repeat itself."""

    conditional: Conditional | None = None
    description: str | None = None


class MetadataOption(BaseModel):
    """One choice's own selector, for a field whose options are separately addressable."""

    model_config = ConfigDict(populate_by_name=True)

    label: str
    selector: str | None


class MetadataField(BaseModel):
    """A control's address within a stage. Technical keys only.

    Three shapes, per 3.4: a plain `selector`; `selectorYes`/`selectorNo` for a
    two-sided gate; or `selector: null` with per-option selectors.
    """

    model_config = ConfigDict(populate_by_name=True)

    questionId: str
    selector: str | None = None
    selectorYes: str | None = None
    selectorNo: str | None = None
    options: list[MetadataOption] | None = None


class Stage(BaseModel):
    """One page of the flow, as the replay script walks it."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    url: str = ""
    pageTitle: str = ""
    stageType: str = "form"
    waitMs: int = 0
    waitForSelector: str | None = None
    fields: list[MetadataField] = []
    next: str | None = None
    """Locator for the control that advances past this stage."""


class Config(BaseModel):
    """`metadata.config`. A null spec makes the corresponding helper a no-op."""

    model_config = ConfigDict(populate_by_name=True)

    quoteUrlPattern: str | None = None
    dismissModals: list[str] = []
    cardSelector: str | None = None
    createSubmissionText: str | None = None
    continueSelector: str | None = None
    classOfBusinessSearchSelector: str | None = None
    mfa: dict | None = None
    saveQuote: dict | None = None
    bindControl: str | None = None
    """Read for its target, never clicked. No agent and no script may bind."""
    bindUrlCapture: dict | None = None


class EligibilityRule(BaseModel):
    """An answer that declines the risk. A decline is an outcome, not a defect."""

    model_config = ConfigDict(populate_by_name=True)

    coverage: str
    reason: str
    question: str
    questionId: str
    canonical: str
    decliningAnswer: str


class BranchExploration(BaseModel):
    """Gate coverage. Every entry carries a questionId; prose-only fails the gate."""

    model_config = ConfigDict(populate_by_name=True)

    gatesWalkedBothSides: list[str] = []
    unexplored: list[dict[str, str]] = []
    reEntriesUsed: int = 0
    notes: list[str] = []


class Blocked(BaseModel):
    """A control the filler could not clear. A stop condition, not a retry."""

    model_config = ConfigDict(populate_by_name=True)

    page: str
    control: str
    questionId: str | None = None
    whatYouTried: str


class MetadataDoc(BaseModel):
    """The metadata artifact. `carrier_questions_metadata.doc`."""

    model_config = ConfigDict(populate_by_name=True)

    carrier: str
    loginUrl: str = ""
    businessType: str
    insuranceType: str

    mappedThroughReview: bool = False
    submitted: bool = False
    reachedQuote: bool = False
    replayResult: Literal["quote", "declined"] | None = None
    stoppedReason: str = ""
    """Empty when clean."""

    config: Config = Field(default_factory=Config)
    stages: list[Stage] = []
    documents: dict | None = None
    eligibilityRules: list[EligibilityRule] = []
    branchExploration: BranchExploration = Field(default_factory=BranchExploration)
    blocked: list[Blocked] = []


class QuestionsDoc(BaseModel):
    """The questions artifact. `carrier_manifest.doc`."""

    model_config = ConfigDict(populate_by_name=True)

    carrier: str
    businessType: str
    insuranceType: str
    questions: list[Question] = []
