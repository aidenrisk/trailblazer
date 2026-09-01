"""
Frontier agent: decision maker for form-filling strategy.

Frontier maintains the "board" (state of gates, what's been walked, what's pending)
and decides ONE action at a time. It communicates only with Loop.

Frontier is stateless across invocations (it receives board state from Loop).
Loop persists the board and orchestrates all agent calls.
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
    Frontier: decides what to do next in a form fill.

    Frontier is called by Loop at two points:
    1. When a new page is discovered (on_page_description)
    2. When the page has been acted upon and changed/settled (on_diff)

    Each call is stateless — the board state comes from Loop, gets updated,
    and returned to Loop for persistence.
    """

    def __init__(self) -> None:
        self.state = FrontierBoardState()

    def on_page_description(
        self, job: str, page: PageDescription, board: FrontierBoard | None = None
    ) -> tuple[FrontierBoard, Assignment]:
        """
        Loop calls this when Scraper returns a new page.
        "Here's what's on the page now. What should we do?"

        Args:
        - job: job ID (for logging)
        - page: PageDescription from Scraper
        - board: existing board state (if any). If None, initialize new board.

        Returns:
        - (updated_board, assignment): the board state and one action for FormFiller
        """
        # Initialize or restore board state
        if board is None:
            self.state.board = FrontierBoard(currentStageId=page.stageId, status="exploring")
        else:
            self.state.board = board

        # Identify gates on this page
        gates = self.state.identify_gates(page)
        if not self.state.board.gates:
            self.state.board.gates = gates

        # Update current stage
        self.state.board.currentStageId = page.stageId

        # Decide next action
        assignment = self.state.next_assignment_for_page(page)

        return (self.state.board, assignment)

    def on_diff(
        self, job: str, diff: Diff, last_assignment: Assignment, board: FrontierBoard
    ) -> tuple[FrontierBoard, Assignment | WalkSlice]:
        """
        Loop calls this after FormFiller executes and page is compared.
        "The page changed (or didn't). Now what?"

        Args:
        - job: job ID
        - diff: what changed on the page
        - last_assignment: what FormFiller just executed
        - board: current board state

        Returns:
        - (updated_board, action): either next Assignment or WalkSlice if complete
        """
        # Restore board state
        self.state.board = board

        # React to diff
        is_walk_complete = self.state.apply_diff(diff, last_assignment)

        # If walk is complete, return slice; otherwise return next assignment
        if is_walk_complete:
            walk_slice = self.state._build_walk_slice(last_assignment)
            return (self.state.board, walk_slice)
        else:
            # Walk still in progress, need to fetch the new page and decide next action
            # Loop will call on_page_description() with the updated page
            # For now, return a placeholder assignment
            return (self.state.board, last_assignment)
