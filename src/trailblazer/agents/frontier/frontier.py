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
    Frontier agent: maintains board state and emits assignments.
    Contract-facing interface matching CLAUDE.md Contracts Summary.
    """

    def __init__(self) -> None:
        self.state = FrontierBoardState()

    def on_page_description(
        self, job: str, page: PageDescription
    ) -> tuple[FrontierBoard, Assignment]:
        """
        Called by Loop after Scraper returns a PageDescription.
        Updates board, emits next Assignment.
        """
        # Initialize or update gates from the page
        gates = self.state.identify_gates(page)
        if not self.state.board.gates:
            self.state.board.gates = gates

        # Update current stage
        self.state.board.currentStageId = page.stageId

        # Decide next assignment
        assignment = self.state.next_assignment_for_page(page)

        return (self.state.board, assignment)

    def on_diff(
        self, job: str, diff: Diff, last_assignment: Assignment
    ) -> tuple[FrontierBoard, Assignment | WalkSlice]:
        """
        Called by Loop after comparing PageDescriptions before/after FormFiller.
        Reacts to diff, may return next Assignment or a WalkSlice.
        """
        action, is_slice = self.state.apply_diff(diff, last_assignment)

        if is_slice:
            # action is a WalkSlice; return it
            return (self.state.board, action)
        else:
            # action is an Assignment; return it
            return (self.state.board, action)
