"""
One attempt to rescue an assignment whose locator missed.

Isolated here, and injected rather than imported, for two reasons. The
deterministic path in form_filler.py stays readable — it never branches on
"is there a model?" — and a filler constructed without a Recovery is provably
offline, which is what lets the whole test suite run free and reproducible.

It fires on locator failures only: `not_found`, `not_unique`, `widget`. Never on
`validation`, because a field rejecting a value is the page talking, and looking
at the page harder will not change its mind.

What comes back is not believed. The filler re-reads the DOM and only reports
success if the value or option is actually there — a model saying it clicked
something is a claim, not evidence.
"""

import logging
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from pydantic import BaseModel, Field

from trailblazer.agents.browser.tools import read_only_tools, write_tools
from trailblazer.agents.form_filler import dom
from trailblazer.contracts import Assignment, FillReport, FillStep, Option
from trailblazer.shared.llm import get_model

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    Path(__file__).parents[2] / "prompts" / "form_filler" / "system.md"
).read_text(encoding="utf-8")

# Locator problems. `validation` is deliberately absent.
RECOVERABLE = ("not_found", "not_unique", "widget")


class RecoveryResult(BaseModel):
    """What the model says it did."""

    ok: bool = Field(description="True only if the action actually landed")
    locator: str | None = Field(
        default=None, description="The selector actually used, for replay"
    )
    value: str | None = Field(default=None, description="The value typed, if any")
    option: str | None = Field(default=None, description="The option label chosen, if any")
    discovered_options: list[str] | None = Field(
        default=None, description="Option labels, if the control turned out to be a chooser"
    )
    note: str = Field(default="", description="One line on what was wrong")


class Recovery:
    """
    Args:
    - max_attempts: how many assignments may be rescued in one walk. A page
      whose locators are all wrong should fail loudly and cheaply, not burn a
      model call per control on the way down.
    """

    def __init__(self, model=None, max_attempts: int = 5) -> None:
        self._model = model
        self.max_attempts = max_attempts
        self.attempts = 0

    def attempt(
        self, job: str, assignment: Assignment, failed: FillReport, filler
    ) -> FillReport:
        if failed.errorClass not in RECOVERABLE:
            return failed
        if self.attempts >= self.max_attempts:
            logger.warning("[%s] recovery budget spent (%d)", job, self.max_attempts)
            return failed
        self.attempts += 1

        page = filler.page
        try:
            agent = self._agent(page)
            result = agent.invoke({"messages": [{"role": "user", "content": self._brief(assignment, failed)}]})
            outcome: RecoveryResult = result["structured_response"]
        except Exception as e:
            logger.warning("[%s] recovery failed to run: %s", job, e)
            return failed

        logger.info(
            "[%s] recovery says ok=%s locator=%r (%s)",
            job, outcome.ok, outcome.locator, outcome.note,
        )
        if not outcome.ok or not outcome.locator:
            return failed

        return self._verify(job, assignment, outcome, page) or failed

    # ------------------------------------------------------------------

    def _agent(self, page):
        from langchain.agents import create_agent

        return create_agent(
            model=self._model or get_model(temperature=0.0),
            tools=read_only_tools(page) + write_tools(page),
            system_prompt=SYSTEM_PROMPT,
            response_format=RecoveryResult,
        )

    @staticmethod
    def _brief(assignment: Assignment, failed: FillReport) -> str:
        lines = [
            f"Assignment type: {assignment.type}",
            f"Field id: {assignment.fieldId}",
            f"Locator given: {assignment.locator}",
            f"It failed with: {failed.errorClass}",
        ]
        if assignment.option is not None:
            lines.append(f"Option to choose: {assignment.option.label}")
            lines.append(f"Option locator given: {assignment.option.locator}")
        lines.append("Land this one action, then stop.")
        return "\n".join(lines)

    @staticmethod
    def _verify(
        job: str, assignment: Assignment, outcome: RecoveryResult, page
    ) -> FillReport | None:
        """
        Check the DOM for what the model claims it did.

        Returns a report only when the page agrees. This is the whole reason a
        model is allowed near the page at all: its output is a hypothesis, and
        the element it names is the test.
        """
        target, error = dom.resolve(page, outcome.locator)
        if target is None:
            logger.warning(
                "[%s] recovery reported %r, which is %s", job, outcome.locator, error
            )
            return None

        if outcome.value is not None:
            try:
                if target.input_value() != outcome.value:
                    logger.warning(
                        "[%s] recovery claimed %r but the field shows %r",
                        job, outcome.value, target.input_value(),
                    )
                    return None
            except PlaywrightError:
                return None

        options = (
            [Option(label=label, locator=outcome.locator) for label in outcome.discovered_options]
            if outcome.discovered_options
            else None
        )
        logger.info("[%s] recovery verified against the page", job)
        return FillReport(
            ok=True,
            steps=[
                FillStep(
                    fieldId=assignment.fieldId,
                    action="select" if outcome.option else "fill",
                    locator=outcome.locator,
                    value=outcome.value or outcome.option,
                )
            ],
            landed=[assignment.fieldId] if assignment.fieldId else [],
            fieldId=assignment.fieldId,
            discoveredOptions=options,
            chosenOption=outcome.option,
        )
