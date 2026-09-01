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
        """Calling on_page_description initializes board and emits first assignment."""
        board, assignment = agent.on_page_description("job1", page_with_gate)

        assert len(board.gates) == 1
        assert board.currentStageId == "page_entity"
        assert isinstance(assignment, SetOptionAssignment)
        assert assignment.type == "set_option"
        assert assignment.option == "LLC"

    def test_updates_current_stage(self, agent, page_with_gate):
        """Each call updates currentStageId."""
        board1, _ = agent.on_page_description("job1", page_with_gate)
        assert board1.currentStageId == "page_entity"

        next_page = page_with_gate.model_copy()
        next_page.stageId = "page_next_stage"
        board2, _ = agent.on_page_description("job1", next_page)
        assert board2.currentStageId == "page_next_stage"


class TestFrontierAgentOnDiff:
    """Test on_diff contract method."""

    def test_positive_diff_returns_assignment(self, agent, page_with_gate):
        """
        +ve diff -> returns next Assignment (not a WalkSlice).
        """
        _, assignment = agent.on_page_description("job1", page_with_gate)
        assert isinstance(assignment, SetOptionAssignment)
        assert assignment.option == "LLC"

        # Apply +ve diff
        diff = Diff(polarity="+ve")
        board, action = agent.on_diff("job1", diff, assignment)

        # Result is an Assignment, not a WalkSlice (is_slice=False)
        assert isinstance(action, SimpleAssignment)
        assert board.status == "exploring"

    def test_negative_diff_returns_walk_slice(self, agent, page_with_gate):
        """
        -ve diff after walking all options -> returns WalkSlice.
        """
        # First option
        _, assignment1 = agent.on_page_description("job1", page_with_gate)
        diff1 = Diff(polarity="+ve")
        agent.on_diff("job1", diff1, assignment1)

        # Second option
        _, assignment2 = agent.on_page_description("job1", page_with_gate)
        assert isinstance(assignment2, SetOptionAssignment)
        assert assignment2.option == "Corp"

        # -ve diff -> walk slice
        diff2 = Diff(polarity="-ve")
        board, action = agent.on_diff("job1", diff2, assignment2)

        assert isinstance(action, list)  # WalkSlice is a list[WalkStep]
        assert board.status == "slice_stable"


class TestFrontierAgentBoardPersistence:
    """Test that agent's board state persists across calls."""

    def test_board_state_maintained(self, agent, page_with_gate):
        """Agent's internal board state is maintained across calls."""
        board1, _ = agent.on_page_description("job1", page_with_gate)
        original_gates = len(board1.gates)

        board2, _ = agent.on_page_description("job1", page_with_gate)
        # On second call with same page, board already has gates
        assert len(board2.gates) >= original_gates
