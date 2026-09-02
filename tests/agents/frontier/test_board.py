"""
Tests for FrontierBoardState — the exploration rules, in isolation.

Everything here drives the board through its real entry points (sync_controls /
absorb_fill_report) rather than assigning board.controls by hand, so the tests
actually cover how controls land on the board.
"""

import pytest

from trailblazer.agents.frontier.board import FrontierBoardState, synthetic_value
from trailblazer.contracts import (
    Control,
    FillFieldAssignment,
    FillReport,
    FrontierBoard,
    Option,
    PageDescription,
    SetOptionAssignment,
)
from tests.agents.frontier.frontier_test_data import (
    PAGE_1_BUSINESS_INFO,
    PAGE_SIMPLE,
    REVEALED_PRONOUNS,
)

MALE = Option(label="Male", locator="#gender-male")
FEMALE = Option(label="Female", locator="#gender-female")


@pytest.fixture
def board_state():
    return FrontierBoardState()


@pytest.fixture
def page():
    return PageDescription(**PAGE_SIMPLE)


@pytest.fixture
def synced(board_state, page):
    """A board that has seen PAGE_SIMPLE once."""
    board_state.sync_controls(page)
    return board_state


def fill(field_id, locator="#x", value="Test Value"):
    return FillFieldAssignment(
        type="fill_field", fieldId=field_id, locator=locator, value=value
    )


def plain_report(field_id):
    """A FillReport for a control that turned out to be a plain field."""
    return FillReport(ok=True, fieldId=field_id, discoveredOptions=None)


def discovery_report(field_id, options, chosen):
    """A FillReport where FormFiller found the control was actually a chooser."""
    return FillReport(
        ok=True, fieldId=field_id, discoveredOptions=options, chosenOption=chosen
    )


class TestSyncControls:
    def test_tracks_every_control_in_page_order(self, board_state, page):
        board_state.sync_controls(page)

        assert [c.fieldId for c in board_state.board.controls] == [
            "q_name",
            "q_gender",
            "q_consent",
            "q_email",
        ]
        assert all(not c.explored for c in board_state.board.controls)

    def test_options_from_page_description_become_pending(self, synced):
        consent = next(c for c in synced.board.controls if c.fieldId == "q_consent")

        assert [o.label for o in consent.options] == ["Yes", "Maybe"]
        assert [o.label for o in consent.pending] == ["Yes", "Maybe"]
        assert consent.walked == []

    def test_unknown_options_stay_unknown(self, synced):
        gender = next(c for c in synced.board.controls if c.fieldId == "q_gender")

        # None means "we don't know yet", NOT "no options".
        assert gender.options is None
        assert gender.pending == []

    def test_is_idempotent(self, synced, page):
        synced.sync_controls(page)
        synced.sync_controls(page)

        assert len(synced.board.controls) == 4

    def test_revealed_control_is_appended(self, synced, page):
        page.controls.append(Control(**REVEALED_PRONOUNS))
        added = synced.sync_controls(page)

        assert [c.fieldId for c in added] == ["q_pronouns"]
        # Appended at the end, so it's explored after everything already queued.
        assert synced.board.controls[-1].fieldId == "q_pronouns"

    def test_picks_up_options_scraper_learned_later(self, synced, page):
        """Step 15: if Scraper itself finds the options, that's just as good."""
        gender_control = next(c for c in page.controls if c.fieldId == "q_gender")
        gender_control.options = [MALE, FEMALE]

        synced.sync_controls(page)
        gender = next(c for c in synced.board.controls if c.fieldId == "q_gender")

        assert [o.label for o in gender.pending] == ["Male", "Female"]

    def test_does_not_reset_progress_on_a_known_chooser(self, synced, page):
        """A later sync must not wipe walked options and cause a re-walk."""
        synced.absorb_fill_report(
            plain_report("q_name"), fill("q_name", "#name")
        )
        consent_assignment = SetOptionAssignment(
            type="set_option",
            fieldId="q_consent",
            key="el_q_consent",
            option="Yes",
            locator="#consent-yes",
            controlLocator="#consent",
        )
        synced.absorb_fill_report(
            FillReport(ok=True, fieldId="q_consent", chosenOption="Yes"),
            consent_assignment,
        )

        synced.sync_controls(page)
        consent = next(c for c in synced.board.controls if c.fieldId == "q_consent")

        assert [o.label for o in consent.walked] == ["Yes"]
        assert [o.label for o in consent.pending] == ["Maybe"]


class TestAbsorbFillReport:
    def test_plain_field_is_explored_after_one_fill(self, synced):
        synced.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        name = next(c for c in synced.board.controls if c.fieldId == "q_name")

        assert name.explored is True
        # [] (not None) records "confirmed: no options".
        assert name.options == []

    def test_discovered_options_are_installed_and_chosen_one_walked(self, synced):
        """Step 10: filler says 'this is a dropdown, I'm trying Female'."""
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Female"),
            fill("q_gender", "#gender"),
        )
        gender = next(c for c in synced.board.controls if c.fieldId == "q_gender")

        assert [o.label for o in gender.options] == ["Male", "Female"]
        assert [o.label for o in gender.walked] == ["Female"]
        assert [o.label for o in gender.pending] == ["Male"]
        # Step 12: NOT explored — Male still has to be tried.
        assert gender.explored is False

    def test_control_is_explored_once_all_options_walked(self, synced):
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Female"),
            fill("q_gender", "#gender"),
        )
        synced.absorb_fill_report(
            FillReport(ok=True, fieldId="q_gender", chosenOption="Male"),
            SetOptionAssignment(
                type="set_option",
                fieldId="q_gender",
                key="el_q_gender",
                option="Male",
                locator="#gender-male",
                controlLocator="#gender",
            ),
        )
        gender = next(c for c in synced.board.controls if c.fieldId == "q_gender")

        # Step 14: now it's explored.
        assert gender.pending == []
        assert [o.label for o in gender.walked] == ["Female", "Male"]
        assert gender.explored is True

    def test_chooser_with_zero_options_is_explored_not_stuck(self, synced):
        """
        [] means "I opened it, it genuinely has no options".

        If this marked the control unexplored it would be re-filled forever and
        every later control on the page would be unreachable.
        """
        synced.absorb_fill_report(
            discovery_report("q_gender", [], None), fill("q_gender", "#gender")
        )
        gender = next(c for c in synced.board.controls if c.fieldId == "q_gender")

        assert gender.explored is True

    def test_failed_report_records_nothing(self, synced):
        synced.absorb_fill_report(
            FillReport(ok=False, fieldId="q_name", errorClass="not_found"),
            fill("q_name", "#name"),
        )
        name = next(c for c in synced.board.controls if c.fieldId == "q_name")

        assert name.explored is False
        assert synced.walk_log == []


class TestSelectControl:
    def test_returns_controls_in_page_order(self, synced, page):
        assert synced.select_control(page).fieldId == "q_name"

        synced.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        assert synced.select_control(page).fieldId == "q_gender"

    def test_pending_options_block_the_next_control(self, synced, page):
        """
        The core rule (step 12): a control with unexplored options must be
        finished before Frontier looks at anything after it.
        """
        synced.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Female"),
            fill("q_gender", "#gender"),
        )

        # Male is still pending, so q_consent/q_email stay out of reach.
        assert synced.select_control(page).fieldId == "q_gender"

    def test_none_when_page_fully_explored(self, synced, page):
        for entry in synced.board.controls:
            entry.explored = True
            entry.pending = []

        assert synced.select_control(page) is None

    def test_ignores_controls_from_other_stages(self, synced, page):
        for entry in synced.board.controls:
            entry.explored = True
            entry.pending = []
        synced.board.controls[0].stageId = "some_other_page"
        synced.board.controls[0].explored = False

        assert synced.select_control(page) is None


class TestAssignmentFor:
    def test_unknown_options_gets_fill_field(self, synced):
        gender = next(c for c in synced.board.controls if c.fieldId == "q_gender")
        assignment = synced.assignment_for(gender)

        assert assignment.type == "fill_field"
        assert assignment.fieldId == "q_gender"
        assert assignment.locator == "#gender"

    def test_pending_options_gets_set_option_with_the_options_own_locator(self, synced):
        """
        The Yes/Maybe requirement: when the option carries its own locator, that
        is what FormFiller must act on — not the parent control's.
        """
        consent = next(c for c in synced.board.controls if c.fieldId == "q_consent")
        assignment = synced.assignment_for(consent)

        assert assignment.type == "set_option"
        assert assignment.fieldId == "q_consent"
        assert assignment.option == "Yes"
        assert assignment.locator == "#consent-yes"  # NOT "#consent"
        assert assignment.controlLocator == "#consent"

    def test_option_with_no_node_passes_none_through(self, synced):
        """
        A native `<select>`'s choices are not clickable: the answer is set with
        `select_option(label)` against the select itself, and the scraper says
        so by leaving `Option.locator` None.

        Frontier must NOT collapse that to the control's locator. Doing so
        looks harmless and silently makes native selects unfillable, because
        FormFiller can no longer tell "click this" from "select on this".
        """
        synced.absorb_fill_report(
            discovery_report(
                "q_gender",
                [
                    Option(label="Male", locator=None),
                    Option(label="Female", locator=None),
                ],
                None,
            ),
            fill("q_gender", "#gender"),
        )
        gender = next(c for c in synced.board.controls if c.fieldId == "q_gender")
        assignment = synced.assignment_for(gender)

        assert assignment.locator is None
        assert assignment.controlLocator == "#gender"
        assert assignment.option == "Male"

    def test_does_not_consume_the_option(self, synced):
        """
        Building an assignment must not mutate pending — the option only counts
        as walked once FormFiller reports it landed. Otherwise a failed fill
        would silently skip an option.
        """
        consent = next(c for c in synced.board.controls if c.fieldId == "q_consent")
        synced.assignment_for(consent)
        synced.assignment_for(consent)

        assert [o.label for o in consent.pending] == ["Yes", "Maybe"]


class TestWalkLog:
    def test_fill_records_a_type_step_with_its_value(self, synced):
        synced.absorb_fill_report(
            plain_report("q_name"), fill("q_name", "#name", "Test Value")
        )

        assert len(synced.walk_log) == 1
        step = synced.walk_log[0]
        assert step.action == "type"
        assert step.fieldId == "q_name"
        assert step.locator == "#name"
        assert step.value == "Test Value"

    def test_choice_records_the_option_and_its_locator(self, synced):
        synced.absorb_fill_report(
            FillReport(ok=True, fieldId="q_consent", chosenOption="Yes"),
            SetOptionAssignment(
                type="set_option",
                fieldId="q_consent",
                key="el_q_consent",
                option="Yes",
                locator="#consent-yes",
                controlLocator="#consent",
            ),
        )

        step = synced.walk_log[0]
        assert step.action == "choose"
        assert step.option == "Yes"
        assert step.locator == "#consent-yes"

    def test_discovery_records_the_option_filler_picked(self, synced):
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Female"),
            fill("q_gender", "#gender"),
        )

        step = synced.walk_log[0]
        assert step.action == "choose"
        assert step.option == "Female"
        assert step.locator == "#gender-female"

    def test_advance_attempt_records_a_click(self, synced):
        synced.note_advance_attempt("simple_page_1", 'button:has-text("Next")')

        assert synced.walk_log[0].action == "click"
        assert synced.walk_log[0].locator == 'button:has-text("Next")'
        assert synced.already_tried_to_advance("simple_page_1")

    def test_unlanded_navigation_is_discarded(self, synced):
        """A Next click that didn't navigate must not stay in the slice."""
        synced.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        synced.note_advance_attempt("simple_page_1", 'button:has-text("Next")')
        synced.discard_unlanded_navigation()

        assert [s.action for s in synced.walk_log] == ["type"]


class TestSyntheticValue:
    @pytest.mark.parametrize(
        "field_id,expected",
        [
            ("q_002", "01/01/2026"),  # Policy Effective Date -> date
            ("q_003", "10001"),  # Business Zip Code -> zip
            ("q_004", "Test Value"),  # Legal Business Name -> generic text
            ("q_007", "12-3456789"),  # FEIN
            ("q_008", "10000"),  # Target or Incumbent Premium -> number
        ],
    )
    def test_matches_the_field_it_fills(self, field_id, expected):
        page = PageDescription(**PAGE_1_BUSINESS_INFO)
        control = next(c for c in page.controls if c.fieldId == field_id)

        assert synthetic_value(control) == expected

    def test_is_injectable(self, page):
        state = FrontierBoardState(value_provider=lambda c: f"<{c.fieldId}>")
        state.sync_controls(page)
        name = next(c for c in state.board.controls if c.fieldId == "q_name")

        assert state.assignment_for(name).value == "<q_name>"


class TestBoardSerialization:
    def test_round_trips_through_json(self, synced):
        restored = FrontierBoard.model_validate_json(synced.board.model_dump_json())

        assert len(restored.controls) == len(synced.board.controls)
        assert restored.currentStageId == synced.board.currentStageId
        assert restored.status == synced.board.status
        consent = next(c for c in restored.controls if c.fieldId == "q_consent")
        assert [o.locator for o in consent.pending] == [
            "#consent-yes",
            "#consent-maybe",
        ]


class TestLiveRevealPriority:
    """
    A field that exists only because of the option currently set must be
    explored before that option changes — otherwise it vanishes and the fill
    fails with not_found.
    """

    def revealed_control(self, equals):
        return Control(
            fieldId="q_extra",
            key="el_q_extra",
            label="Extra",
            type="text",
            required=False,
            options=None,
            locator="#extra",
            unique=True,
            revealedBy={"fieldId": "q_gender", "equals": equals},
        )

    def test_live_reveal_beats_a_pending_option(self, synced, page):
        synced.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Male"),
            fill("q_gender", "#gender"),
        )
        # Choosing Male revealed a field. Female is still pending on q_gender.
        page.controls.append(self.revealed_control("Male"))
        synced.sync_controls(page)

        # Without the priority rule this would return q_gender (pending Female),
        # and #extra would be gone by the time we got to it.
        assert synced.select_control(page).fieldId == "q_extra"

    def test_stale_reveal_does_not_jump_the_queue(self, synced, page):
        synced.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Male"),
            fill("q_gender", "#gender"),
        )
        # A field revealed by Female, while Male is what's actually set.
        page.controls.append(self.revealed_control("Female"))
        synced.sync_controls(page)

        assert synced.select_control(page).fieldId == "q_gender"

    def test_unconditional_control_never_jumps_the_queue(self, synced, page):
        synced.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Male"),
            fill("q_gender", "#gender"),
        )

        assert synced.select_control(page).fieldId == "q_gender"


class TestBuildWalk:
    def test_no_choosers_gives_exactly_one_path(self, board_state, page):
        """A form with nothing to branch on has one path through it."""
        plain_page = page.model_copy(deep=True)
        plain_page.controls = [c for c in plain_page.controls if c.fieldId == "q_name"]
        board_state.sync_controls(plain_page)
        board_state.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))

        walk = board_state.build_walk()

        assert len(walk.paths) == 1
        assert walk.paths[0].choices == {}
        assert [s.fieldId for s in walk.paths[0].steps] == ["q_name"]

    def test_one_chooser_gives_one_path_per_option(self, synced):
        synced.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Male"),
            fill("q_gender", "#gender"),
        )
        synced.absorb_fill_report(
            FillReport(ok=True, fieldId="q_gender", chosenOption="Female"),
            SetOptionAssignment(
                type="set_option",
                fieldId="q_gender",
                key="el_q_gender",
                option="Female",
                locator="#gender-female",
                controlLocator="#gender",
            ),
        )

        walk = synced.build_walk()

        assert [p.choices for p in walk.paths] == [
            {"q_gender": "Male"},
            {"q_gender": "Female"},
        ]
        # Each path keeps its own option and drops the other.
        assert [s.option for s in walk.paths[0].steps if s.action == "choose"] == ["Male"]
        assert [s.option for s in walk.paths[1].steps if s.action == "choose"] == ["Female"]
        # The shared fill is on both.
        for path in walk.paths:
            assert any(s.fieldId == "q_name" for s in path.steps)

    def test_paths_preserve_observed_order(self, synced):
        """
        Every path must be a subsequence of a real execution — we never invent
        an ordering that wasn't observed.
        """
        synced.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Male"),
            fill("q_gender", "#gender"),
        )
        synced.absorb_fill_report(plain_report("q_email"), fill("q_email", "#email"))

        observed = [s.fieldId for s in synced.walk_log]
        for path in synced.build_walk().paths:
            fields = [s.fieldId for s in path.steps]
            positions = [observed.index(f) for f in fields]
            assert positions == sorted(positions)

    def test_reveal_from_a_non_chooser_is_treated_as_unconditional(
        self, board_state, page
    ):
        """
        If a field is revealed by something we never walked options for, we
        can't pin it to a branch. Include it everywhere rather than dropping it
        from every path — losing a real step is worse than an extra one.
        """
        page.controls.append(
            Control(
                fieldId="q_extra",
                key="el_q_extra",
                label="Extra",
                type="text",
                required=False,
                options=None,
                locator="#extra",
                unique=True,
                # q_name is a plain text field, not a chooser.
                revealedBy={"fieldId": "q_name", "equals": "Test Value"},
            )
        )
        board_state.sync_controls(page)
        board_state.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        board_state.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Male"),
            fill("q_gender", "#gender"),
        )
        board_state.absorb_fill_report(plain_report("q_extra"), fill("q_extra", "#extra"))

        for path in board_state.build_walk().paths:
            assert any(s.fieldId == "q_extra" for s in path.steps)

    def test_walk_log_stays_the_observed_order(self, synced):
        """walk_log is the raw record; build_walk() is the replayable view."""
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Male"),
            fill("q_gender", "#gender"),
        )
        synced.absorb_fill_report(
            FillReport(ok=True, fieldId="q_gender", chosenOption="Female"),
            SetOptionAssignment(
                type="set_option",
                fieldId="q_gender",
                key="el_q_gender",
                option="Female",
                locator="#gender-female",
                controlLocator="#gender",
            ),
        )

        # Both options in the log...
        assert [s.option for s in synced.walk_log] == ["Male", "Female"]
        # ...but one per path.
        for path in synced.build_walk().paths:
            assert len([s for s in path.steps if s.action == "choose"]) == 1


class TestRevealedChooser:
    """
    A revealed control that is ITSELF a chooser.

    This is where the live-reveal rule earns its keep twice over: the control
    disappears when its enabling option changes, AND it has its own options that
    all have to be walked before that happens.
    """

    STYLE = Control(
        fieldId="q_style",
        key="el_q_style",
        label="Style",
        type="select",
        required=False,
        options=[
            Option(label="Full", locator="#style-full"),
            Option(label="Goatee", locator="#style-goatee"),
        ],
        locator="#style",
        unique=True,
        revealedBy={"fieldId": "q_gender", "equals": "Male"},
    )

    def reveal(self, synced, page):
        """Walk Male on q_gender, which reveals the nested chooser."""
        synced.absorb_fill_report(plain_report("q_name"), fill("q_name", "#name"))
        synced.absorb_fill_report(
            discovery_report("q_gender", [MALE, FEMALE], "Male"),
            fill("q_gender", "#gender"),
        )
        page.controls.append(self.STYLE.model_copy(deep=True))
        synced.sync_controls(page)
        return synced

    def test_revealed_chooser_with_known_options_still_takes_priority(
        self, synced, page
    ):
        """
        Regression: the priority pass used to skip anything with pending
        options, so a revealed chooser arrived with its options already known
        and got walked LAST — after q_gender had moved to Female and #style no
        longer existed.
        """
        self.reveal(synced, page)

        target = synced.select_control(page)
        assert target.fieldId == "q_style"
        assert target.pending, "options were already known, which is the regression"

    def test_all_of_its_options_walked_before_the_enabling_option_changes(
        self, synced, page
    ):
        self.reveal(synced, page)

        # Both q_style options...
        for label in ("Full", "Goatee"):
            assert synced.select_control(page).fieldId == "q_style"
            synced.absorb_fill_report(
                FillReport(ok=True, fieldId="q_style", chosenOption=label),
                SetOptionAssignment(
                    type="set_option",
                    fieldId="q_style",
                    key="el_q_style",
                    option=label,
                    locator=f"#style-{label.lower()}",
                    controlLocator="#style",
                ),
            )

        # ...only then does q_gender move on to Female.
        assert synced.select_control(page).fieldId == "q_gender"

        observed = [(s.fieldId, s.option) for s in synced.walk_log]
        assert observed == [
            ("q_name", None),
            ("q_gender", "Male"),
            ("q_style", "Full"),
            ("q_style", "Goatee"),
        ]

    def test_choices_only_reports_what_a_path_actually_chooses(self, synced, page):
        """
        Regression: `choices` was the pinned dict used for filtering, so the
        Female path claimed "q_style": "Full" despite never touching q_style —
        q_style doesn't exist on that branch.
        """
        self.reveal(synced, page)
        for label in ("Full", "Goatee"):
            synced.absorb_fill_report(
                FillReport(ok=True, fieldId="q_style", chosenOption=label),
                SetOptionAssignment(
                    type="set_option",
                    fieldId="q_style",
                    key="el_q_style",
                    option=label,
                    locator=f"#style-{label.lower()}",
                    controlLocator="#style",
                ),
            )
        synced.absorb_fill_report(
            FillReport(ok=True, fieldId="q_gender", chosenOption="Female"),
            SetOptionAssignment(
                type="set_option",
                fieldId="q_gender",
                key="el_q_gender",
                option="Female",
                locator="#gender-female",
                controlLocator="#gender",
            ),
        )

        walk = synced.build_walk()
        female = next(p for p in walk.paths if p.choices.get("q_gender") == "Female")

        assert "q_style" not in female.choices
        assert not any(s.fieldId == "q_style" for s in female.steps)

        # And every path's choices match the choices in its own steps.
        for path in walk.paths:
            from_steps = {
                s.fieldId: s.option for s in path.steps if s.action == "choose"
            }
            assert path.choices == from_steps

    def test_variants_that_collapse_are_not_duplicated(self, synced, page):
        """
        Pinning q_style on a branch where it doesn't exist yields the same
        script as not pinning it. Identical steps mean an identical Program, so
        only one path should come out.
        """
        self.reveal(synced, page)
        for label in ("Full", "Goatee"):
            synced.absorb_fill_report(
                FillReport(ok=True, fieldId="q_style", chosenOption=label),
                SetOptionAssignment(
                    type="set_option",
                    fieldId="q_style",
                    key="el_q_style",
                    option=label,
                    locator=f"#style-{label.lower()}",
                    controlLocator="#style",
                ),
            )
        synced.absorb_fill_report(
            FillReport(ok=True, fieldId="q_gender", chosenOption="Female"),
            SetOptionAssignment(
                type="set_option",
                fieldId="q_gender",
                key="el_q_gender",
                option="Female",
                locator="#gender-female",
                controlLocator="#gender",
            ),
        )

        walk = synced.build_walk()
        scripts = [
            tuple((s.action, s.fieldId, s.option) for s in p.steps)
            for p in walk.paths
        ]
        assert len(scripts) == len(set(scripts)), "duplicate programs"
