"""What Loop hands the Generator, and what the Generator has produced so far.

Generation is incremental: the Generator is called after every fill, not once
per finished page, and appends to all three artifacts in one step. There is no
walk slice -- the files on disk are the accumulation, so no agent holds a page's
action sequence in state.

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


class GenerationState(BaseModel):
    """What has been written so far. Returned so Loop can assert completion."""

    model_config = ConfigDict(populate_by_name=True)

    questionIds: list[str] = []
    """Allocated question ids, in order. `q_001` onward, stable across the flow.

    Distinct from `Control.fieldId`, which is a per-page counter reset at every
    perceive. The artifacts join on this one, so the Generator owns allocation.
    """

    stages: list[str] = []
    """Stage names written to the metadata artifact, in order."""

    scriptSteps: int = 0
    """Blocks written to the replay script. Compared against `stages` at every
    page boundary: a stage in the metadata and not in the script is the defect
    that let a gate exist in both manifests and in neither script."""

    unresolvedCanonicals: list[str] = []
    """Canonical keys the script reads that no question supplies."""
