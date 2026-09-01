import pytest

from trailblazer.agents.frontier.board import FrontierBoardState
from trailblazer.contracts import (
    ChangedControl,
    Control,
    Diff,
    PageDescription,
    SetOptionAssignment,
    SimpleAssignment,
)


@pytest.fixture
def board_state():
    """Provide a fresh FrontierBoardState for each test."""
    return FrontierBoardState()


@pytest.fixture
def simple_page_no_gates():
    """A page with no gates, just required fields."""
    return PageDescription(
        stageId="page_1",
        url="https://example.com/page1",
        controls=[
            Control(
                fieldId="q_001",
                label="Business Name",
                type="text",
                required=True,
                locator="#businessName",
                unique=True,
            ),
        ],
        next="button:has-text('Next')",
        back=None,
        candidateGates=[],
        blockers=[],
    )


@pytest.fixture
def page_with_one_gate():
    """A page with one gate (select with 2 options)."""
    return PageDescription(
        stageId="page_2_entity_type",
        url="https://example.com/page2",
        controls=[
            Control(
                fieldId="q_entity_type",
                label="Entity Type",
                type="select",
                required=True,
                options=["LLC", "Corporation"],
                locator="#entityType",
                unique=True,
            ),
            Control(
                fieldId="q_002",
                label="Business Name",
                type="text",
                required=True,
                locator="#businessName",
                unique=True,
            ),
        ],
        next="button:has-text('Next')",
        back=None,
        candidateGates=["q_entity_type"],
        blockers=[],
    )


class TestIdentifyGates:
    """Test gate identification from PageDescription."""

    def test_no_gates_when_no_candidates(self, board_state, simple_page_no_gates):
        """Page with no candidateGates should yield no gates."""
        gates = board_state.identify_gates(simple_page_no_gates)
        assert gates == []

    def test_single_gate_from_select(self, board_state, page_with_one_gate):
        """A select control with 2+ options in candidateGates -> one gate."""
        gates = board_state.identify_gates(page_with_one_gate)
        assert len(gates) == 1
        gate = gates[0]
        assert gate.gateId == "g_q_entity_type"
        assert gate.fieldId == "q_entity_type"
        assert gate.options == ["LLC", "Corporation"]
        assert gate.walked == []
        assert gate.pending == ["LLC", "Corporation"]
        assert gate.kind == "same-page"  # page has a "next" button

    def test_gate_kind_last_page(self, board_state):
        """Gate on a page with no next button -> kind='last-page'."""
        page = PageDescription(
            stageId="final",
            url="https://example.com/final",
            controls=[
                Control(
                    fieldId="q_choice",
                    label="Choice",
                    type="toggle",
                    required=True,
                    options=["Option A", "Option B"],
                    locator="#choice",
                    unique=True,
                ),
            ],
            next=None,
            back=None,
            candidateGates=["q_choice"],
            blockers=[],
        )
        gates = board_state.identify_gates(page)
        assert len(gates) == 1
        assert gates[0].kind == "last-page"

    def test_ignore_controls_with_less_than_two_options(self, board_state):
        """Select controls with <2 options are not gates."""
        page = PageDescription(
            stageId="page",
            url="https://example.com",
            controls=[
                Control(
                    fieldId="q_single",
                    label="Only One",
                    type="select",
                    required=True,
                    options=["Only Option"],
                    locator="#single",
                    unique=True,
                ),
            ],
            next=None,
            back=None,
            candidateGates=["q_single"],
            blockers=[],
        )
        gates = board_state.identify_gates(page)
        assert gates == []


class TestNextAssignmentForPage:
    """Test assignment decision logic."""

    def test_page_with_blockers_stops(self, board_state, page_with_one_gate):
        """Page with blockers -> status=blocked, emit stop."""
        page = page_with_one_gate.model_copy()
        page.blockers = ["Validation error", "Overlay"]
        assignment = board_state.next_assignment_for_page(page)
        assert assignment.type == "stop"
        assert board_state.board.status == "blocked"

    def test_gate_with_pending_emits_set_option(
        self, board_state, page_with_one_gate
    ):
        """Page with a gate that has pending options -> emit set_option."""
        gates = board_state.identify_gates(page_with_one_gate)
        board_state.board.gates = gates
        board_state.board.currentStageId = page_with_one_gate.stageId

        assignment = board_state.next_assignment_for_page(page_with_one_gate)

        assert isinstance(assignment, SetOptionAssignment)
        assert assignment.type == "set_option"
        assert assignment.gateId == "g_q_entity_type"
        assert assignment.option == "LLC"  # first option popped
        assert assignment.locator == "#entityType"
        assert board_state.board.status == "awaiting_fill"
        # Gate's pending should shrink
        assert board_state.board.gates[0].pending == ["Corporation"]

    def test_all_gates_walked_emits_next_after_fill(self, board_state, page_with_one_gate):
        """When all gates are walked, fill unfilled required fields, then next."""
        gates = board_state.identify_gates(page_with_one_gate)
        gates[0].walked = ["LLC", "Corporation"]
        gates[0].pending = []
        board_state.board.gates = gates
        board_state.board.currentStageId = page_with_one_gate.stageId

        assignment = board_state.next_assignment_for_page(page_with_one_gate)

        # page_with_one_gate has q_002 (Business Name) unfilled, so emit fill_page
        assert assignment.type == "fill_page"

        # After fill is done (applicant slice populated), next call emits next
        page_filled = page_with_one_gate.model_copy()
        page_filled.controls[1].required = False  # Mark q_002 as filled (optional)
        gates[0].pending = []
        assignment2 = board_state.next_assignment_for_page(page_filled)

        assert isinstance(assignment2, SimpleAssignment)
        assert assignment2.type == "next"
        assert board_state.board.status == "advancing"

    def test_no_gates_unfilled_required_fields_emits_fill_page(
        self, board_state, simple_page_no_gates
    ):
        """Page with no gates but unfilled required fields -> emit fill_page."""
        board_state.board.currentStageId = simple_page_no_gates.stageId

        assignment = board_state.next_assignment_for_page(simple_page_no_gates)

        assert assignment.type == "fill_page"
        assert "q_001" in assignment.applicantSlice


class TestApplyDiff:
    """Test diff reaction logic."""

    def test_positive_diff_after_set_option_updates_walked(
        self, board_state, page_with_one_gate
    ):
        """
        +ve diff after set_option -> move option from pending to walked.
        """
        gates = board_state.identify_gates(page_with_one_gate)
        board_state.board.gates = gates
        board_state.board.currentStageId = page_with_one_gate.stageId

        # Get first assignment (set_option for LLC)
        assignment = board_state.next_assignment_for_page(page_with_one_gate)
        assert isinstance(assignment, SetOptionAssignment)
        assert assignment.option == "LLC"

        # Apply a +ve diff (page changed after setting LLC)
        diff = Diff(polarity="+ve")
        action, is_slice = board_state.apply_diff(diff, assignment)

        # Option should be in walked
        assert board_state.board.gates[0].walked == ["LLC"]
        assert board_state.board.gates[0].pending == ["Corporation"]
        assert board_state.board.status == "exploring"
        assert not is_slice

    def test_negative_diff_after_last_option_returns_walk_slice(
        self, board_state, page_with_one_gate
    ):
        """
        -ve diff after the last option is set -> status=slice_stable, return WalkSlice.
        """
        gates = board_state.identify_gates(page_with_one_gate)
        board_state.board.gates = gates
        board_state.board.currentStageId = page_with_one_gate.stageId

        # Walk the first option
        assignment1 = board_state.next_assignment_for_page(page_with_one_gate)
        diff1 = Diff(polarity="+ve")
        board_state.apply_diff(diff1, assignment1)

        # Walk the second option
        assignment2 = board_state.next_assignment_for_page(page_with_one_gate)
        assert isinstance(assignment2, SetOptionAssignment)
        assert assignment2.option == "Corporation"

        # Apply -ve diff (page settled)
        diff2 = Diff(polarity="-ve")
        action, is_slice = board_state.apply_diff(diff2, assignment2)

        assert is_slice
        assert board_state.board.status == "slice_stable"
        # v0: walk_slice is empty (populated in v1)
        assert action == []


class TestBoardSerialization:
    """Test board round-tripping through JSON."""

    def test_frontier_board_round_trip(self, board_state, page_with_one_gate):
        """FrontierBoard can be serialized and deserialized cleanly."""
        gates = board_state.identify_gates(page_with_one_gate)
        board_state.board.gates = gates
        board_state.board.currentStageId = page_with_one_gate.stageId

        # Serialize and deserialize
        json_str = board_state.board.model_dump_json()
        restored = board_state.board.model_validate_json(json_str)

        assert restored.gates == board_state.board.gates
        assert restored.currentStageId == board_state.board.currentStageId
        assert restored.status == board_state.board.status
