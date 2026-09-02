"""
Frontier agent: decides the single next action in a form walk.

Frontier explores EVERY control on a page, one at a time, in page order. It does
not try to guess in advance which controls branch. A control turns out to be a
chooser either because Scraper reported its options or because FormFiller
discovered them while filling it — and either way, every option gets walked
before Frontier moves to the next control.

Fully deterministic: no LLM. The only judgment call is what value to type into a
plain field, and that's injected (`value_provider`), so an LLM-backed picker can
replace it without touching the graph.

State lives HERE. Frontier is the only agent that needs the board, so Loop
doesn't pass it in or out — Loop just calls on_page() and gets one Assignment
(or, when the walk is over, the WalkSlice).

Implemented as a LangGraph StateGraph:

    absorb_feedback -> sync_controls -> select_control --+- "act" -> emit_assignment -> END
                                                        |
                                                        +- "done" -> finish_page -> END
"""

import logging
from typing import TypedDict

from langgraph.graph import END, StateGraph

from trailblazer.agents.frontier.board import FrontierBoardState, ValueProvider
from trailblazer.contracts import (
    Assignment,
    ControlState,
    ScraperResult,
    FillReport,
    PageDescription,
    SimpleAssignment,
    Walk,
    WalkSlice,
)

logger = logging.getLogger(__name__)


class FrontierState(TypedDict):
    """
    Per-invocation transport for the graph.

    The durable board is NOT in here — it lives on FrontierAgent.state and the
    nodes mutate it directly. This dict only carries what this one call is about
    (the page, and any feedback from the last action) plus the outcome.
    """

    job: str
    page: PageDescription
    scrape: ScraperResult | None
    fill_report: FillReport | None
    last_assignment: Assignment | None
    target: ControlState | None
    outcome: Assignment | Walk | None


class FrontierAgent:
    """
    Frontier: decides what to do next in a form walk.

    Loop calls on_page() every time it has a fresh PageDescription, passing along
    the ScraperResult and FillReport from the last action (both None on the
    first call).
    Frontier returns either one Assignment for FormFiller, or the finished
    Walk (one replayable path per branch) when the whole form has been walked.
    
    """

    def __init__(self, value_provider: ValueProvider | None = None) -> None:
        self.state = FrontierBoardState(value_provider=value_provider)
        self._last_assignment: Assignment | None = None
        self.graph = self._build_graph()

    # ------------------------------------------------------------------
    # Public entry point (the only one)
    # ------------------------------------------------------------------

    def on_page(
        self,
        job: str,
        page: PageDescription,
        scrape: ScraperResult | None = None,
        fill_report: FillReport | None = None,
    ) -> Assignment | Walk:
        """
        "Here's the page as it is now, and here's what happened last time."

        Args:
        - job: job ID (for logging)
        - page: fresh PageDescription from Scraper
        - scrape: the scraper's result for this look — the page plus what
                  changed since the prior one (None on the first call)
        - fill_report: what FormFiller did and discovered (None on the first call)

        Returns:
        - Assignment: the one thing FormFiller should do next, or
        - Walk: one replayable WalkPath per branch, when the walk is complete
        """
        initial: FrontierState = {
            "job": job,
            "page": page,
            "scrape": scrape,
            "fill_report": fill_report,
            "last_assignment": self._last_assignment,
            "target": None,
            "outcome": None,
        }
        final = self.graph.invoke(initial)
        outcome = final["outcome"]

        # Remember what we just asked for, so the next call can attribute the
        # FillReport to the right control without Loop having to echo it back.
        self._last_assignment = None if isinstance(outcome, Walk) else outcome
        return outcome

    @property
    def board(self):
        """The current board. Read-only view for logging/debugging."""
        return self.state.board

    @property
    def walk_log(self) -> WalkSlice:
        """
        Every landed step in observed order, branches interleaved.

        For logging and assertions. NOT replayable — a chooser's options are
        walked in place, so this holds all of them. Use the returned Walk for
        replayable per-branch paths.
        """
        return self.state.walk_log

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self):
        graph = StateGraph(FrontierState)

        graph.add_node("absorb_feedback", self._absorb_feedback)
        graph.add_node("sync_controls", self._sync_controls)
        graph.add_node("select_control", self._select_control)
        graph.add_node("emit_assignment", self._emit_assignment)
        graph.add_node("finish_page", self._finish_page)

        graph.set_entry_point("absorb_feedback")
        graph.add_edge("absorb_feedback", "sync_controls")
        graph.add_edge("sync_controls", "select_control")

        # The one real branch: is there still a control to work on?
        graph.add_conditional_edges(
            "select_control",
            lambda s: "act" if s["target"] is not None or s["page"].blockers else "done",
            {"act": "emit_assignment", "done": "finish_page"},
        )
        graph.add_edge("emit_assignment", END)
        graph.add_edge("finish_page", END)

        return graph.compile()

    # ------------------------------------------------------------------
    # Nodes (thin wrappers over board.py logic)
    # ------------------------------------------------------------------

    def _absorb_feedback(self, state: FrontierState) -> FrontierState:
        """Fold the last action's results into the board."""
        report = state["fill_report"]
        if report is not None:
            self.state.absorb_fill_report(report, state["last_assignment"])

        scrape = state["scrape"]
        if scrape is not None:
            logger.info(
                "[%s] diff %s (+%d/-%d controls)",
                state["job"],
                scrape.polarity,
                len(scrape.addedControls),
                len(scrape.removedControls),
            )

        self.state.board.status = "exploring"
        return state

    def _sync_controls(self, state: FrontierState) -> FrontierState:
        """Track new controls (including ones just revealed) and any new options."""
        # sync_controls records currentStageId itself.
        self.state.sync_controls(state["page"])
        return state

    def _select_control(self, state: FrontierState) -> FrontierState:
        """Choose the one control to work on, or nothing if the page is done."""
        page = state["page"]
        if page.blockers:
            self.state.board.status = "blocked"
            state["target"] = None
            logger.warning("[%s] page blocked: %s", state["job"], page.blockers)
            return state

        state["target"] = self.state.select_control(page)
        return state

    def _emit_assignment(self, state: FrontierState) -> FrontierState:
        """One control -> one Assignment."""
        if self.state.board.status == "blocked":
            state["outcome"] = SimpleAssignment(type="stop")
            return state

        target = state["target"]
        assignment = self.state.assignment_for(target)
        self.state.board.status = "awaiting_fill"
        logger.info(
            "[%s] %s -> %s", state["job"], target.fieldId, assignment.model_dump()
        )
        state["outcome"] = assignment
        return state

    def _finish_page(self, state: FrontierState) -> FrontierState:
        """
        Every control on this page is explored. Advance, or publish the walk.

        Three cases:
        - There's a Next button we haven't clicked yet -> click it.
        - We already clicked Next here and we're STILL on this stage -> the click
          didn't navigate (validation held us, or it wasn't really a Next). The
          page has settled, which per the contract means the walk is done. Publish
          rather than clicking Next forever.
        - No Next button -> that was the last page. Publish.

        Publishing means reconstructing one replayable path per branch out of
        the single in-place action log. See board.build_walk().
        """
        page = state["page"]

        if page.next and not self.state.already_tried_to_advance(page.stageId):
            self.state.note_advance_attempt(page.stageId, page.next)
            self.state.board.status = "advancing"
            logger.info("[%s] %s fully explored -> next", state["job"], page.stageId)
            state["outcome"] = SimpleAssignment(type="next")
            return state

        if page.next:
            # Clicked Next, went nowhere. Take the click back out of the walk log
            # so the slice only contains actions that actually landed.
            self.state.discard_unlanded_navigation()
            self.state.board.status = "slice_stable"
            logger.warning(
                "[%s] still on %s after clicking next; walk settled",
                state["job"],
                page.stageId,
            )
        else:
            self.state.board.status = "complete"

        walk = self.state.build_walk()
        logger.info(
            "[%s] walk finished: %d actions -> %d replayable paths",
            state["job"],
            len(self.state.action_log),
            len(walk.paths),
        )
        state["outcome"] = walk
        return state
