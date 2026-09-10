"""Per-agent step and cost accounting for one crawl.

`CostTracker` posts one ledger row per completed LLM call. Agent work steps
(a look, a fill, an assign) are recorded separately at zero USD so spend is
not counted twice. `total_usd` is the sum of every row.

A crawl walks a form page by page and every wrong turn costs a model call, so
"which agent is burning the budget" is the first question asked of a run that
came out expensive. Without a run-level view the answer is spread across
hundreds of log lines.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from trailblazer.observability.logging import get_logger

log = get_logger(__name__)


@dataclass
class Step:
    """One unit of work by one agent."""

    agent: str
    action: str
    """What was done: a work action (`fill`, `assign`, `append`) or an LLM
    call site (`perceive`, `choose_value`, `vision`)."""

    detail: str = ""
    """The target, when there is one: a fieldId, a stageId, an outcome."""

    usd: float = 0.0
    """USD for an LLM-call row. Zero on work steps: those make no call of
    their own, and Frontier and the Generator are deterministic."""

    unpriced: bool = False
    """A call was made but could not be priced: an Anthropic model missing from
    the local table. Distinct from a step that legitimately cost nothing."""

    ms: int = 0
    ok: bool = True


@dataclass
class RunLedger:
    """Every step of one crawl, aggregated per agent.

    Held by Loop and passed to each agent, so an agent records its own work
    without knowing about the others.
    """

    job_id: str
    steps: list[Step] = field(default_factory=list)
    _started: float = field(default_factory=time.monotonic)

    def record(
        self,
        agent: str,
        action: str,
        detail: str = "",
        usd: float = 0.0,
        ms: int = 0,
        ok: bool = True,
        unpriced: bool = False,
    ) -> None:
        """Add one step and log it."""
        self.steps.append(Step(agent, action, detail, usd, unpriced, ms, ok))
        log.info(
            "step job_id=%s agent=%s action=%s detail=%s usd=%s ms=%d ok=%s",
            self.job_id,
            agent,
            action,
            detail or "-",
"unknown" if unpriced else f"{usd:.6f}",
            ms,
            ok,
        )

    def by_agent(self) -> dict[str, dict[str, Any]]:
        """Steps, cost and failures per agent."""
        out: dict[str, dict[str, Any]] = {}
        for s in self.steps:
            a = out.setdefault(s.agent, {"steps": 0, "usd": 0.0, "ms": 0, "failed": 0, "unpriced": 0})
            a["steps"] += 1
            a["ms"] += s.ms
            if not s.ok:
                a["failed"] += 1
            a["usd"] += s.usd
            if s.unpriced:
                a["unpriced"] += 1
        return out

    def total_usd(self) -> float:
        """Sum of every priced step. Unpriced steps are counted separately."""
        return sum(s.usd for s in self.steps)

    def summary(self) -> dict[str, Any]:
        """The whole run, for logging at the end and for the job result."""
        return {
            "job_id": self.job_id,
            "steps": len(self.steps),
            "usd": round(self.total_usd(), 6),
            "unpriced": sum(1 for s in self.steps if s.unpriced),
            "elapsed_ms": int((time.monotonic() - self._started) * 1000),
            "by_agent": self.by_agent(),
        }

    def log_summary(self) -> None:
        """One line per agent, then the total."""
        for agent, a in sorted(self.by_agent().items()):
            log.info(
                "agent total job_id=%s agent=%s steps=%d usd=%.6f ms=%d failed=%d",
                self.job_id,
                agent,
                a["steps"],
                a["usd"],
                a["ms"],
                a["failed"],
            )
        log.info(
            "run total job_id=%s steps=%d usd=%.6f elapsed_ms=%d",
            self.job_id,
            len(self.steps),
            self.total_usd(),
            int((time.monotonic() - self._started) * 1000),
        )
