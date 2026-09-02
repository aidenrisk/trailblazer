"""Loop: orchestrates the agents to walk a form end-to-end.

Implemented as a LangGraph StateGraph. Each node wraps exactly one agent call
(matching the "agents never call each other, only Loop calls agents" rule).

    frontier_decide -> formfiller_execute -> perceive -> frontier_decide
           |                     |
      walk / stop             not ok
           v                     v
          END                   END

Two things Loop carries that no agent does:

- **The FillReport.** That is how FormFiller's discoveries ("this control was
  actually a dropdown, here are its options") reach Frontier without the two
  agents talking directly.
- **`page_index` and the prior PageDescription.** The scraper is stateless
  between looks, so Loop holds the counter and the previous description; the
  scraper needs both to compute the diff and to attribute `revealedBy`.

Loop does not diff -- the scraper owns that and returns a `ScraperResult`.
Loop does not hold Frontier's board either -- Frontier owns it.
"""

import logging
import uuid
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.contracts import (
    Assignment,
    FillFieldAssignment,
    FillReport,
    PageDescription,
    PerceiveRequest,
    ScraperResult,
    SetOptionAssignment,
    Walk,
)
from trailblazer.shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

# One look at one page, already bound to a browser page. Loop stays free of
# Playwright this way: the caller binds `perceive` to a live Page (see
# `run_crawl`), and Loop just asks for descriptions.
Perceiver = Callable[[PerceiveRequest], ScraperResult]

# Nodes burned per control explored: decide -> execute -> perceive. LangGraph's
# default recursion_limit of 25 runs out about eight controls in, so derive a
# real budget. Walks are bounded by controls x options x pages, none of which we
# know up front, so this is a generous ceiling that still fails fast on a cycle.
DEFAULT_RECURSION_LIMIT = 400


class LoopState(TypedDict):
    job: str
    page_index: int
    current_page: PageDescription
    prior_page: PageDescription | None
    assignment: Assignment | None
    last_assignment: Assignment | None
    fill_report: FillReport | None
    scrape: ScraperResult | None
    result: Walk | None


def assignment_values(assignment: Assignment | None) -> dict[str, str] | None:
    """Flatten an Assignment to the fieldId -> value map the scraper wants.

    `PerceiveRequest.assignment` is what makes `revealedBy` answerable: the
    scraper knows which controls are new, and this says what was just submitted
    to make them appear. Navigation carries no field value, so it is None.
    """
    if isinstance(assignment, FillFieldAssignment):
        return {assignment.fieldId: assignment.value}
    if isinstance(assignment, SetOptionAssignment):
        return {assignment.fieldId: assignment.option}
    return None


class Loop:
    def __init__(
        self,
        perceiver: Perceiver,
        frontier: FrontierAgent,
        formfiller: Any,
        replaygen: Any = None,
        validator: Any = None,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    ) -> None:
        self.perceiver = perceiver
        self.frontier = frontier
        self.formfiller = formfiller
        self.replaygen = replaygen
        self.validator = validator
        self.recursion_limit = recursion_limit
        self.graph = self._build_graph()

    def fill_form(self, job: str, initial_page: PageDescription) -> Walk:
        """Walk a form by orchestrating the agents.

        Args:
        - job: job id, for logging
        - initial_page: the first PageDescription (from a first perceive)

        Returns:
        - Walk: one replayable WalkPath per branch, ready for ReplayGen
        """
        initial_state: LoopState = {
            "job": job,
            "page_index": 1,
            "current_page": initial_page,
            "prior_page": None,
            "assignment": None,
            "last_assignment": None,
            "fill_report": None,
            "scrape": None,
            "result": None,
        }
        final_state = self.graph.invoke(
            initial_state, config={"recursion_limit": self.recursion_limit}
        )
        return final_state["result"] or Walk()

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self):
        graph = StateGraph(LoopState)

        graph.add_node("frontier_decide", self._frontier_decide)
        graph.add_node("formfiller_execute", self._formfiller_execute)
        graph.add_node("perceive", self._perceive)

        graph.set_entry_point("frontier_decide")

        graph.add_conditional_edges(
            "frontier_decide",
            self._after_decide,
            {"stop": END, "continue": "formfiller_execute"},
        )
        graph.add_conditional_edges(
            "formfiller_execute",
            lambda s: "continue" if s["fill_report"].ok else "stop",
            {"stop": END, "continue": "perceive"},
        )
        graph.add_edge("perceive", "frontier_decide")

        return graph.compile()

    @staticmethod
    def _after_decide(state: LoopState) -> str:
        # Frontier published the paths: we are done.
        if state["result"] is not None:
            return "stop"
        # Frontier gave up (blocked page, or nothing it can do).
        if state["assignment"].type == "stop":
            return "stop"
        return "continue"

    # ------------------------------------------------------------------
    # Node implementations (each wraps exactly one agent call)
    # ------------------------------------------------------------------

    def _frontier_decide(self, state: LoopState) -> LoopState:
        action = self.frontier.on_page(
            state["job"],
            state["current_page"],
            state["scrape"],
            state["fill_report"],
        )

        if isinstance(action, Walk):
            state["result"] = action
        else:
            state["assignment"] = action

        # Feedback consumed. Clear it so a re-entry cannot absorb the same
        # FillReport twice and double-count a walked option.
        state["scrape"] = None
        state["fill_report"] = None
        return state

    def _formfiller_execute(self, state: LoopState) -> LoopState:
        fill_report = self.formfiller.execute(
            state["job"], state["current_page"].stageId, state["assignment"]
        )
        state["fill_report"] = fill_report
        state["last_assignment"] = state["assignment"]
        if not fill_report.ok:
            logger.error(
                "[%s] fill failed (%s), stopping walk",
                state["job"],
                fill_report.errorClass,
            )
        return state

    def _perceive(self, state: LoopState) -> LoopState:
        last = state["last_assignment"]

        # Navigation is the only thing that moves us to a different page, and so
        # the only thing that advances the counter the scraper keys on.
        if getattr(last, "type", None) in ("next", "back"):
            state["page_index"] += 1

        result = self.perceiver(
            PerceiveRequest(
                job_id=state["job"],
                page_index=state["page_index"],
                prior=state["current_page"],
                assignment=assignment_values(last),
            )
        )

        state["prior_page"] = state["current_page"]
        state["current_page"] = result.page
        state["scrape"] = result
        return state


# ----------------------------------------------------------------------
# Live entry point
# ----------------------------------------------------------------------


def run_crawl(
    carrier_id: str,
    url: str,
    insurance_types: list[str],
    business_types: list[str],
    frontier: FrontierAgent | None = None,
    formfiller: Any = None,
    headed: bool = False,
    settings: Settings | None = None,
) -> Walk:
    """Crawl one carrier portal and return every path Frontier walked.

    Owns the browser session and binds `perceive` to the live page, so `Loop`
    itself never touches Playwright.

    `insurance_types` and `business_types` shape the objective handed to the
    model and are carried for logging.
    """
    settings = settings or get_settings()
    job_id = uuid.uuid4().hex[:12]
    logger.info(
        "crawl start job_id=%s carrier_id=%s url=%s insurance_types=%s business_types=%s",
        job_id,
        carrier_id,
        url,
        ",".join(insurance_types),
        ",".join(business_types),
    )

    objective = (
        f"Describe this form page. The application is for "
        f"{', '.join(insurance_types) or 'any'} insurance for a "
        f"{', '.join(business_types) or 'general'} business."
    )

    with BrowserSession(
        cdp_port=settings.cdp_port, headed=headed or settings.headed
    ) as session:
        page = session.goto(url)

        def perceiver(request: PerceiveRequest) -> ScraperResult:
            if request.objective is None:
                request = request.model_copy(update={"objective": objective})
            return perceive(page, request, settings)

        first = perceiver(
            PerceiveRequest(job_id=job_id, page_index=1, objective=objective)
        )
        loop = Loop(perceiver, frontier or FrontierAgent(), formfiller)
        walk = loop.fill_form(job_id, first.page)

    logger.info(
        "crawl end job_id=%s stage_id=%s paths=%d",
        job_id,
        first.page.stageId,
        len(walk.paths),
    )
    return walk
