"""
Loop: orchestrates all agents to walk a form end-to-end.

Implemented as a LangGraph StateGraph. Each node wraps exactly one agent call
(matching the "agents never call each other, only Loop calls agents" rule).

    frontier_decide -> formfiller_execute -> rescrape -> frontier_decide
           |                     |
      result / stop           not ok
           v                     v
          END                   END

Loop no longer carries the board: Frontier owns its own state, since it's the
only agent that needs it. What Loop does carry is the FillReport -- that's how
FormFiller's discoveries reach Frontier without the two agents talking directly.
Loop doesn't diff either; Scraper returns the Diff alongside the fresh page.

`run_crawl` is the seam the HTTP API and CLI call. Today it opens the tab and
takes the first look; the full walk through `Loop.fill_form` joins it once a
FormFiller that drives a live page exists (the stub never touches the browser).
"""

import logging
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.scraper import Scraper
from trailblazer.contracts import (
    Assignment,
    Diff,
    FillReport,
    PageDescription,
    ScraperResult,
    Walk,
)
from trailblazer.shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Nodes burned per control explored: decide -> execute -> rescrape. LangGraph's
# default recursion_limit of 25 runs out about eight controls in, so derive a
# real budget instead. Walks are bounded by controls x options x pages, none of
# which we know up front, so this is a generous ceiling that still fails fast on
# a genuine cycle.
DEFAULT_RECURSION_LIMIT = 400


class LoopState(TypedDict):
    job: str
    current_page: PageDescription
    assignment: Assignment | None
    last_assignment: Assignment | None
    fill_report: FillReport | None
    diff: Diff | None
    result: Walk | None


class Loop:
    def __init__(
        self,
        scraper: Any,
        frontier: FrontierAgent,
        formfiller: Any,
        replaygen: Any = None,
        validator: Any = None,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    ) -> None:
        self.scraper = scraper
        self.frontier = frontier
        self.formfiller = formfiller
        self.replaygen = replaygen
        self.validator = validator
        self.recursion_limit = recursion_limit
        self.graph = self._build_graph()

    def fill_form(self, job: str, initial_page: PageDescription) -> Walk:
        """
        Walk a form by orchestrating all agents.

        Args:
        - job: job ID (for logging/tracking)
        - initial_page: first PageDescription (page to start from)

        Returns:
        - Walk: one replayable WalkPath per branch, ready for ReplayGen
        """
        initial_state: LoopState = {
            "job": job,
            "current_page": initial_page,
            "assignment": None,
            "last_assignment": None,
            "fill_report": None,
            "diff": None,
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
        graph.add_node("rescrape", self._rescrape)

        graph.set_entry_point("frontier_decide")

        graph.add_conditional_edges(
            "frontier_decide",
            self._after_decide,
            {"stop": END, "continue": "formfiller_execute"},
        )
        graph.add_conditional_edges(
            "formfiller_execute",
            lambda s: "continue" if s["fill_report"].ok else "stop",
            {"stop": END, "continue": "rescrape"},
        )
        graph.add_edge("rescrape", "frontier_decide")

        return graph.compile()

    @staticmethod
    def _after_decide(state: LoopState) -> str:
        # Frontier returned the finished walk: we're done.
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
            state["diff"],
            state["fill_report"],
        )

        if isinstance(action, Walk):
            state["result"] = action
        else:
            state["assignment"] = action
            if action.type == "stop":
                logger.warning(
                    "[%s] frontier stopped on %s (%s)",
                    state["job"],
                    state["current_page"].stageId,
                    getattr(action, "reason", None) or "no reason given",
                )

        # Feedback consumed. Clear it so a re-entry can't absorb the same
        # FillReport twice and double-count a walked option.
        state["diff"] = None
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

    def _rescrape(self, state: LoopState) -> LoopState:
        page, diff = self.scraper.look(
            state["job"],
            "post_fill",
            state["last_assignment"],
            state["fill_report"],
        )
        state["current_page"] = page
        state["diff"] = diff
        return state


def run_crawl(
    carrier_id: str,
    url: str,
    insurance_types: list[str],
    business_types: list[str],
    headed: bool = False,
    settings: Settings | None = None,
) -> ScraperResult:
    """Open the carrier's portal and return what the Scraper saw on the first page.

    `insurance_types` and `business_types` are carried for logging and for the
    objective handed to the model; nothing routes on them until FormFiller can
    drive the live tab and `Loop.fill_form` takes over from here.
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
        f"Describe this form page. The application is for {', '.join(insurance_types) or 'any'} "
        f"insurance for a {', '.join(business_types) or 'general'} business."
    )

    with BrowserSession(cdp_port=settings.cdp_port, headed=headed or settings.headed) as session:
        page = session.goto(url)
        scraper = Scraper(page, settings, objective=objective)
        scraper.look(job_id)
        result = scraper.last_result

    assert result is not None  # look() always stores the perceive it just did
    logger.info(
        "crawl end job_id=%s stage_id=%s polarity=%s",
        job_id,
        result.page.stageId,
        result.polarity,
    )
    return result
