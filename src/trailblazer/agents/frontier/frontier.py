"""
Frontier agent: the contract-facing interface to the board logic.

This module wraps FrontierBoardState to provide two clean, simple methods
that Loop calls at the right times. Think of FrontierAgent as the "public API"
and FrontierBoardState as the internal implementation.

Why split it this way?
- FrontierBoardState focuses on pure logic and state (easy to test, easy to reason about)
- FrontierAgent is the thin wrapper that handles the interaction protocol with Loop
  (simpler to change protocol later without touching core logic)
"""

from trailblazer.agents.frontier.board import FrontierBoardState
from trailblazer.contracts import (
    Assignment,
    Diff,
    FrontierBoard,
    PageDescription,
    WalkSlice,
)


class FrontierAgent:
    """
    Frontier agent: contract-facing interface.

    This agent is responsible for deciding what FormFiller should do next,
    given what's on the page and how the page changed after the last action.

    Public interface:
    - on_page_description(): "Here's a new page, what should we do?"
    - on_diff(): "The page changed (or didn't), now what?"

    Both methods are called by Loop at the right times in the orchestration.
    """

    def __init__(self) -> None:
        # Internal state manager (pure logic, no I/O).
        self.state = FrontierBoardState()

    def on_page_description(
        self, job: str, page: PageDescription
    ) -> tuple[FrontierBoard, Assignment]:
        """
        Called by Loop when Scraper has analyzed a page.
        Input: job ID (for logging), PageDescription (what's on the page)
        Output: (updated FrontierBoard, next Assignment for FormFiller)

        Process:
        1. Extract gates from candidateGates in the page (branching points)
        2. Update board's current stage
        3. Decide the next single action (set_option, fill_page, next, etc.)
        4. Return the board (so Loop can persist it) and the assignment (so FormFiller knows what to do)
        """
        # Step 1: Are there any branching points on this page?
        gates = self.state.identify_gates(page)
        # Only set gates once (on first page encounter). On revisits, keep previous gate state.
        if not self.state.board.gates:
            self.state.board.gates = gates

        # Step 2: Remember where we are.
        self.state.board.currentStageId = page.stageId

        # Step 3: Decide the next move given the current page.
        assignment = self.state.next_assignment_for_page(page)

        # Step 4: Return the updated board and the assignment.
        # Loop will:
        # - Save the board (so if it crashes, it knows the history)
        # - Give the assignment to FormFiller
        # - After FormFiller executes, compare pages and call on_diff()
        return (self.state.board, assignment)

    def on_diff(
        self, job: str, diff: Diff, last_assignment: Assignment
    ) -> tuple[FrontierBoard, Assignment | WalkSlice]:
        """
        Called by Loop after comparing page states before/after FormFiller.
        Input: job ID, what changed (Diff), what FormFiller just did (last_assignment)
        Output: (updated FrontierBoard, next Assignment OR a WalkSlice for ReplayGen)

        The Diff tells us whether the click had an effect:
        - "+ve" (positive): page changed (e.g., new fields revealed)
                          → continue exploring this gate option
        - "-ve" (negative): page settled (e.g., field validation blocked navigation)
                          → finalize this walk, send to ReplayGen

        Returns an Assignment most of the time, but when a walk is complete (slice_stable),
        returns a WalkSlice instead for ReplayGen to compile into a script.
        """
        # Let the board logic decide how to react to the diff.
        action, is_slice = self.state.apply_diff(diff, last_assignment)

        # Return the updated board and the action.
        # If is_slice=True, action is a WalkSlice (list of steps).
        # If is_slice=False, action is an Assignment (next instruction for FormFiller).
        return (self.state.board, action)
