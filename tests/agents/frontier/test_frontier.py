import pytest

from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.contracts import (
    Control,
    Diff,
    PageDescription,
    SetOptionAssignment,
    SimpleAssignment,
)


@pytest.fixture
def agent():
    """Provide a fresh FrontierAgent for each test."""
    return FrontierAgent()


@pytest.fixture
def page_with_gate():
    """A page with one gate."""
    return PageDescription(
        stageId="page_entity",
        url="https://example.com/entity",
        controls=[
            Control(
                fieldId="q_entity",
                label="Entity Type",
                type="select",
                required=True,
                options=["LLC", "Corp"],
                locator="#entityType",
                unique=True,
            ),
        ],
        next="button:has-text('Next')",
        back=None,
        candidateGates=["q_entity"],
        blockers=[],
    )


class TestFrontierAgentOnPageDescription:
    """Test on_page_description contract method."""

    def test_initializes_board_with_gates(self, agent, page_with_gate):
        """on_page_description initializes board and emits first assignment."""
        board, assignment = agent.on_page_description("job1", page_with_gate)

        assert len(board.gates) == 1
        assert board.currentStageId == "page_entity"
        assert isinstance(assignment, SetOptionAssignment)
        assert assignment.option == "LLC"

    def test_restores_board_state(self, agent, page_with_gate):
        """on_page_description can restore existing board state."""
        # First call: initialize board
        board1, assignment1 = agent.on_page_description("job1", page_with_gate)
        assert len(board1.gates) == 1

        # Second call: restore board state
        page_updated = page_with_gate.model_copy()
        page_updated.stageId = "page_2"
        board2, assignment2 = agent.on_page_description("job1", page_updated, board1)

        # Board should retain gates from first call
        assert len(board2.gates) >= 1
        assert board2.currentStageId == "page_2"


class TestFrontierAgentOnDiff:
    """Test on_diff contract method."""

    def test_positive_diff_returns_assignment(self, agent, page_with_gate):
        """
        +ve diff after set_option → walk continues.
        """
        board, assignment = agent.on_page_description("job1", page_with_gate)
        assert isinstance(assignment, SetOptionAssignment)
        assert assignment.option == "LLC"

        # Apply +ve diff
        diff = Diff(polarity="+ve")
        updated_board, action = agent.on_diff("job1", diff, assignment, board)

        # Board should have option marked as walked
        assert "LLC" in updated_board.gates[0].walked
        assert updated_board.status == "exploring"

    def test_negative_diff_returns_walk_slice(self, agent, page_with_gate):
        """
        -ve diff after walking all options → returns WalkSlice.
        """
        board, assignment1 = agent.on_page_description("job1", page_with_gate)

        # Walk first option
        diff1 = Diff(polarity="+ve")
        board, _ = agent.on_diff("job1", diff1, assignment1, board)

        # Get second option
        board, assignment2 = agent.on_page_description("job1", page_with_gate, board)
        assert isinstance(assignment2, SetOptionAssignment)
        assert assignment2.option == "Corp"

        # Walk second option with -ve diff (complete)
        diff2 = Diff(polarity="-ve")
        board, action = agent.on_diff("job1", diff2, assignment2, board)

        # Should return WalkSlice
        assert isinstance(action, list)
        assert board.status == "slice_stable"
