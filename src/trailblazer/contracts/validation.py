"""What the Validator is asked to check, and what it found.

The Validator runs a generated replay script against a fixture and reports the
outcome. The vocabulary is closed and is RoadRunner's, not ours: the runner
normalizes anything unrecognized, and a decline is a SUCCESSFUL run.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Outcome = Literal["quote", "appetite-decline", "stuck"]
"""`quote` reached a priced quote. `appetite-decline` was refused by the carrier
on eligibility -- a correct result, not a defect. `stuck` is everything else."""


class ValidationRequest(BaseModel):
    """One run of one script against one set of answers."""

    model_config = ConfigDict(populate_by_name=True)

    job_id: str
    script_path: str
    answers_path: str
    """`{client, answers}` or a bare answers object, as the runner passes it."""

    headed: bool = False


class ValidationResult(BaseModel):
    """The run's outcome, in the shape the replay runner's status contract needs."""

    model_config = ConfigDict(populate_by_name=True)

    outcome: Outcome
    reachedQuote: bool
    stoppedReason: str | None = None

    premium: float | None = None
    premiumDisplay: str | None = None
    quoteNumber: str | None = None
    quoteUrl: str | None = None

    bindUrl: str | None = None
    """Read from the control's target. NEVER by clicking it."""

    bindControlLabel: str | None = None
    exitCode: int = 1
    """0 for `quote` or `appetite-decline`, 1 otherwise."""

    @property
    def success(self) -> bool:
        """A decline is a successful run; only `stuck` is a failure."""
        return self.reachedQuote or self.outcome == "appetite-decline"
