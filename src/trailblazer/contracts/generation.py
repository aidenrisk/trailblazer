"""What Loop hands the Generator, and what the Generator has produced so far.

Generation is incremental: the Generator is called after every fill, not once
per finished page, and appends to all three artifacts in one step. The files on
disk are the accumulation, so no agent holds a page's action sequence in state.

A request carries the `walk` it belongs to, because backtracking answers the
same field once per walk and only one walk's answers are a path the form
actually rendered.

The artifacts themselves are the external boundary and are specified in
`.sessions/03-architecture-and-spec.md` sections 3.3 and 3.4. This module is
only the internal input contract.
"""

from pydantic import BaseModel, ConfigDict

from trailblazer.contracts.assignment import FillReport
from trailblazer.contracts.page_description import PageDescription


class GenerationRequest(BaseModel):
    """One appended step: what was done, and what the page looked like after."""

    model_config = ConfigDict(populate_by_name=True)

    job_id: str

    carrier: str
    businessType: str
    insuranceType: str
    """The flow this crawl covers. Fixes the artifacts' identity triple."""

    page: PageDescription
    """The page as it stands after the action. `stageId` names the stage."""

    report: FillReport
    """The action to append. Only ever a completed one -- a fill that was
    rejected and corrected arrives once, carrying the corrected value."""

    control_label: str | None = None
    """The acting control's label, when the report names a fieldId.

    Loop resolves it from the page so the Generator need not re-find the control
    whose fieldId the report carries.
    """

    walk: int = 1
    """Which pass over the page this fill belongs to. `Board.walk`.

    Backtracking walks a gate's second side after renavigating and replaying the
    prefix, so one field is answered once per walk. Answers assembled per field
    across different walks are not a path the form ever rendered: q_010's answer
    from the walk where q_009 was Yes survives into the record even after q_009
    is set back to No. The Generator keeps each walk's answers separately and
    publishes one of them as the `exampleValue` set.
    """


class GenerationState(BaseModel):
    """What has been written so far. Returned so Loop can assert completion."""

    model_config = ConfigDict(populate_by_name=True)

    questionIds: list[str] = []
    """Allocated question ids, in order. `q_001` onward, stable across the flow.

    Distinct from `Control.fieldId`, which is issued when a control first
    appears. This one is issued on first fill, which is what the artifacts join
    on, so the Generator owns allocation.
    """

    stages: list[str] = []
    """Stage names written to the metadata artifact, in order."""

    scriptSteps: int = 0
    """Blocks written to the replay script. Compared against `stages` at every
    page boundary: a stage in the metadata and not in the script is the defect
    that let a gate exist in both manifests and in neither script."""

    unresolvedCanonicals: list[str] = []
    """Canonical keys the script reads that no question supplies."""
