"""
Tests for FrontierAgent — the graph, driven the way Loop drives it.

No Loop, no stubs: these call on_page() directly and hand back FillReports by
hand, so the exact assignment sequence is visible and asserted.
"""

import pytest

from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.contracts import (
    Control,
    ScraperResult,
    FillReport,
    Option,
    PageDescription,
    Walk,
)
from tests.agents.frontier.frontier_test_data import (
    PAGE_1_BUSINESS_INFO,
    PAGE_SIMPLE,
    PAGE_SIMPLE_2,
    PIE_DISCOVERABLE,
    REVEALED_PRONOUNS,
)

JOB = "job_test"

# What the scraper reports. Frontier reads only polarity and the added/removed
# lists off this, so a bare result is enough to drive it.
SETTLED = ScraperResult(
    page=PageDescription(**PAGE_SIMPLE),
    polarity="-ve",
    addedControls=[],
    removedControls=[],
    changedControls=[],
)
CHANGED = SETTLED.model_copy(update={"polarity": "+ve"})
MALE = Option(label="Male", locator="#gender-male")
FEMALE = Option(label="Female", locator="#gender-female")


@pytest.fixture
def agent():
    return FrontierAgent()


@pytest.fixture
def page():
    return PageDescription(**PAGE_SIMPLE)


def landed(field_id, options=None, chosen=None):
    """The FillReport Loop would hand back after a successful fill."""
    return FillReport(
        ok=True, fieldId=field_id, discoveredOptions=options, chosenOption=chosen
    )


class TestWalkthrough:
    """The Name / Gender / Email walkthrough, step by step."""

    def test_starts_with_the_first_control(self, agent, page):
        assignment = agent.on_page(JOB, page)

        assert assignment.type == "fill_field"
        assert assignment.fieldId == "q_name"
        assert assignment.value == "Test Value"

    def test_moves_to_the_second_control(self, agent, page):
        agent.on_page(JOB, page)
        assignment = agent.on_page(JOB, page, SETTLED, landed("q_name"))

        assert assignment.type == "fill_field"
        assert assignment.fieldId == "q_gender"

    def test_discovered_options_must_all_be_walked_before_advancing(self, agent, page):
        """
        Steps 10-14: the filler reports Gender is a dropdown and it tried
        Female. Frontier must come back for Male, NOT skip to q_consent.
        """
        agent.on_page(JOB, page)
        agent.on_page(JOB, page, SETTLED, landed("q_name"))

        assignment = agent.on_page(
            JOB,
            page,
            SETTLED,
            landed("q_gender", options=[MALE, FEMALE], chosen="Female"),
        )

        assert assignment.type == "set_option"
        assert assignment.fieldId == "q_gender"
        assert assignment.option == "Male"
        assert assignment.locator == "#gender-male"
        assert assignment.controlLocator == "#gender"

    def test_full_sequence(self, agent, page):
        """The whole page, as one readable trace."""
        sequence = []
        report = None

        for _ in range(12):
            action = agent.on_page(JOB, page, SETTLED, report)
            if isinstance(action, Walk):
                sequence.append(("walk_slice", len(action)))
                break

            if action.type == "fill_field":
                sequence.append((action.type, action.fieldId))
                # Gender is secretly a dropdown; everything else is plain.
                if action.fieldId == "q_gender":
                    report = landed("q_gender", options=[MALE, FEMALE], chosen="Female")
                else:
                    report = landed(action.fieldId)
            elif action.type == "set_option":
                sequence.append((action.type, action.fieldId, action.option))
                report = landed(action.fieldId, chosen=action.option)
            else:
                sequence.append((action.type,))
                break

        assert sequence == [
            ("fill_field", "q_name"),
            ("fill_field", "q_gender"),  # filler discovers Male/Female, picks Female
            ("set_option", "q_gender", "Male"),  # must finish Gender first
            ("set_option", "q_consent", "Yes"),  # options came from the PD
            ("set_option", "q_consent", "Maybe"),
            ("fill_field", "q_email"),
            ("next",),  # page done
        ]

    def test_option_locators_come_from_the_options_not_the_control(self, agent, page):
        """q_consent's options each carry their own locator; use those."""
        locators = []
        report = None

        for _ in range(12):
            action = agent.on_page(JOB, page, SETTLED, report)
            if isinstance(action, Walk) or action.type == "next":
                break
            if action.type == "set_option" and action.fieldId == "q_consent":
                locators.append(action.locator)
                report = landed("q_consent", chosen=action.option)
            elif action.type == "set_option":
                report = landed(action.fieldId, chosen=action.option)
            elif action.fieldId == "q_gender":
                report = landed("q_gender", options=[MALE, FEMALE], chosen="Female")
            else:
                report = landed(action.fieldId)

        assert locators == ["#consent-yes", "#consent-maybe"]


class TestRevealedControls:
    def test_revealed_control_is_explored_before_the_page_finishes(self, agent, page):
        """Step 14: new fields that appear must be explored too."""
        seen = []
        report = None

        for _ in range(14):
            action = agent.on_page(JOB, page, SETTLED, report)
            if isinstance(action, Walk) or action.type == "next":
                break

            if action.type == "set_option":
                # Choosing a Gender reveals the pronouns field.
                if action.fieldId == "q_gender" and not any(
                    c.fieldId == "q_pronouns" for c in page.controls
                ):
                    page.controls.append(Control(**REVEALED_PRONOUNS))
                report = landed(action.fieldId, chosen=action.option)
            elif action.fieldId == "q_gender":
                report = landed("q_gender", options=[MALE, FEMALE], chosen="Female")
            else:
                report = landed(action.fieldId)
            seen.append(action.fieldId)

        assert "q_pronouns" in seen
        # Appended, so it comes after the controls that were already queued.
        assert seen.index("q_pronouns") > seen.index("q_email")


class TestPageCompletion:
    def test_walk_when_last_page_is_fully_explored(self, agent):
        page = PageDescription(**PAGE_SIMPLE_2)  # next=None
        report = None
        action = None

        for _ in range(8):
            action = agent.on_page(JOB, page, SETTLED, report)
            if isinstance(action, Walk):
                break
            report = landed(action.fieldId)

        assert isinstance(action, Walk)
        # Nothing on this page branches, so there is exactly one path.
        assert len(action.paths) == 1
        assert action.paths[0].choices == {}
        assert [s.fieldId for s in action.paths[0].steps] == ["q_phone", "q_start"]
        assert agent.board.status == "complete"

    def test_every_path_carries_values_and_one_option_per_chooser(self, agent, page):
        report = None
        action = None
        second = PageDescription(**PAGE_SIMPLE_2)
        current = page

        for _ in range(16):
            action = agent.on_page(JOB, current, SETTLED, report)
            if isinstance(action, Walk):
                break
            if action.type == "next":
                current = second
                report = FillReport(ok=True, advance=True)
                continue
            if action.type == "set_option":
                report = landed(action.fieldId, chosen=action.option)
            elif action.fieldId == "q_gender":
                report = landed("q_gender", options=[MALE, FEMALE], chosen="Female")
            else:
                report = landed(action.fieldId)

        assert isinstance(action, Walk)

        # Two choosers, two options each: baseline + one variant per extra
        # option = 3 paths. NOT 2 x 2 = 4 (no combinations of independent gates).
        assert len(action.paths) == 3
        assert [p.choices for p in action.paths] == [
            {"q_gender": "Female", "q_consent": "Yes"},
            {"q_gender": "Male", "q_consent": "Yes"},
            {"q_gender": "Female", "q_consent": "Maybe"},
        ]

        for path in action.paths:
            types = [s for s in path.steps if s.action == "type"]
            chooses = [s for s in path.steps if s.action == "choose"]

            # Every typed step carries what landed, so ReplayGen can compile it.
            assert all(s.value for s in types)
            assert {s.fieldId for s in types} == {
                "q_name",
                "q_email",
                "q_phone",
                "q_start",
            }
            # Exactly one choice per chooser — never both options of one.
            assert [s.fieldId for s in chooses] == ["q_gender", "q_consent"]
            assert [s.option for s in chooses] == [
                path.choices["q_gender"],
                path.choices["q_consent"],
            ]
            assert [s.locator for s in path.steps if s.action == "click"] == [
                'button:has-text("Next")'
            ]

    def test_advances_to_the_next_page(self, agent, page):
        for entry_page in (page,):
            report = None
            for _ in range(12):
                action = agent.on_page(JOB, entry_page, SETTLED, report)
                if action.type == "next":
                    break
                if action.type == "set_option":
                    report = landed(action.fieldId, chosen=action.option)
                elif action.fieldId == "q_gender":
                    report = landed("q_gender", options=[MALE, FEMALE], chosen="Female")
                else:
                    report = landed(action.fieldId)

        assert agent.board.status == "advancing"

        second = PageDescription(**PAGE_SIMPLE_2)
        action = agent.on_page(
            JOB, second, CHANGED, FillReport(ok=True, advance=True)
        )

        assert action.type == "fill_field"
        assert action.fieldId == "q_phone"
        assert agent.board.currentStageId == "simple_page_2"


class TestBlockers:
    def test_blocked_page_stops(self, agent, page):
        page.blockers = ["Please correct the errors below"]
        assignment = agent.on_page(JOB, page)

        assert assignment.type == "stop"
        assert agent.board.status == "blocked"


class TestMasterPageDescription:
    """
    The real Pie Insurance scrape: 9 controls, captured live, nothing hand-tuned.

    `discover` below stands in for FormFiller: q_001 and q_006 are custom
    dropdowns that Scraper reported as options: null, so the filler is what finds
    their options.
    """

    def drive(self, agent, page, limit=40):
        """
        Run the walk to completion, acting as Loop + FormFiller would.

        Returns the ordered list of assignments Frontier emitted, plus the final
        outcome (a Walk, or the terminal assignment).
        """
        emitted = []
        report = None

        for _ in range(limit):
            action = agent.on_page(JOB, page, SETTLED, report)
            if isinstance(action, Walk):
                return emitted, action

            emitted.append(action)
            if action.type == "set_option":
                report = landed(action.fieldId, chosen=action.option)
            elif action.type == "fill_field":
                found = PIE_DISCOVERABLE.get(action.fieldId)
                if found and not any(
                    a.type == "set_option" and a.fieldId == action.fieldId
                    for a in emitted
                ):
                    options = [Option(**o) for o in found]
                    report = landed(action.fieldId, options=options, chosen=options[0].label)
                else:
                    report = landed(action.fieldId)
            elif action.type == "next":
                # The stub has no page after this one, so the next look lands on
                # the same stage and Frontier settles. Keep driving.
                report = FillReport(ok=True, advance=True)
            else:
                return emitted, action

        raise AssertionError(f"walk did not terminate in {limit} calls")

    def test_explores_the_first_control(self, agent):
        page = PageDescription(**PAGE_1_BUSINESS_INFO)
        assignment = agent.on_page(JOB, page)

        assert assignment.type == "fill_field"
        assert assignment.fieldId == "q_001"
        assert assignment.locator == "#agencyProgram"

    def test_meta_block_is_ignored_not_an_error(self):
        """
        The live payload carries a `_meta` provenance block that isn't part of
        the contract. It must parse and be dropped, not blow up.
        """
        page = PageDescription(**PAGE_1_BUSINESS_INFO)

        assert "_meta" in PAGE_1_BUSINESS_INFO
        assert not hasattr(page, "_meta")
        assert "_meta" not in page.model_dump()

    def test_tracks_every_control_regardless_of_candidate_gates(self, agent):
        """
        candidateGates names only q_009, but q_001 and q_006 are dropdowns too.
        The old design would have explored one control and missed two branches;
        this one explores all nine.
        """
        page = PageDescription(**PAGE_1_BUSINESS_INFO)
        agent.on_page(JOB, page)

        assert page.candidateGates == ["q_009"]
        assert [c.fieldId for c in agent.board.controls] == [
            f"q_00{i}" for i in range(1, 10)
        ]

    def test_options_in_pd_use_their_own_locator(self, agent):
        """
        q_009 is a split control: its own locator is the SAME string as the "Yes"
        option's. Selecting "No" via the control locator would re-click "Yes".
        """
        page = PageDescription(**PAGE_1_BUSINESS_INFO)
        emitted, _ = self.drive(agent, page)

        q_009 = [a for a in emitted if getattr(a, "fieldId", None) == "q_009"]
        assert [(a.option, a.locator) for a in q_009] == [
            ("Yes", 'internal:label="Yes"i'),
            ("No", 'internal:label="No"i'),
        ]
        # The control locator collides with the Yes option — proof the "No"
        # assignment could not have come from the control locator.
        assert q_009[1].controlLocator == 'internal:label="Yes"i'
        assert q_009[1].locator != q_009[1].controlLocator

    def test_discovered_dropdowns_are_walked_in_full(self, agent):
        """q_006 has three options once opened; all three must be walked."""
        page = PageDescription(**PAGE_1_BUSINESS_INFO)
        self.drive(agent, page)

        entity = next(c for c in agent.board.controls if c.fieldId == "q_006")
        assert [o.label for o in entity.walked] == [
            "Limited Liability Company",
            "Corporation",
            "Sole Proprietor",
        ]
        assert entity.pending == []

    def test_walks_the_whole_page_then_advances(self, agent):
        page = PageDescription(**PAGE_1_BUSINESS_INFO)
        emitted, outcome = self.drive(agent, page)

        assert isinstance(outcome, Walk)
        assert emitted[-1].type == "next"
        assert all(c.explored for c in agent.board.controls)

        # The observed log: 6 plain fields typed, 7 option choices (2+3+2).
        # This is the in-place record, branches interleaved.
        actions = [s.action for s in agent.walk_log]
        assert actions.count("type") == 6
        assert actions.count("choose") == 7
        # No click: Next was pressed but the stub has no page after this one, so
        # it didn't navigate and the unlanded click was dropped.
        assert actions.count("click") == 0
        assert agent.board.status == "slice_stable"

    def test_paths_do_not_multiply_out_the_choosers(self, agent):
        """
        Three choosers with 2/3/2 options. MASTER.md: "do not walk combinations
        of independent gates" — so 1 + 1 + 2 + 1 = 5 paths, not 2 x 3 x 2 = 12.
        """
        page = PageDescription(**PAGE_1_BUSINESS_INFO)
        _, walk = self.drive(agent, page)

        assert isinstance(walk, Walk)
        assert len(walk.paths) == 5

        for path in walk.paths:
            chooses = [s for s in path.steps if s.action == "choose"]
            # One choice per chooser on every path.
            assert [s.fieldId for s in chooses] == ["q_001", "q_006", "q_009"]

    def test_synthetic_values_match_the_real_field_types(self, agent):
        page = PageDescription(**PAGE_1_BUSINESS_INFO)
        self.drive(agent, page)

        typed = {s.fieldId: s.value for s in agent.walk_log if s.action == "type"}
        assert typed == {
            "q_002": "01/01/2026",  # Policy Effective Date
            "q_003": "10001",  # Business Zip Code
            "q_004": "Test Value",  # Legal Business Name
            "q_005": "Test Value",  # DBA
            "q_007": "12-3456789",  # FEIN
            "q_008": "10000",  # Target or Incumbent Premium
        }
