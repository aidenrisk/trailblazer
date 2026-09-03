"""
End-to-end tests: Loop driving the real Frontier against the two stubs.

This is the first test that exercises the whole contract chain —
Frontier -> Assignment -> FormFiller -> FillReport -> Loop -> Scraper -> Diff ->
Frontier — with nothing hand-fed. Fully offline: no LLM, no Playwright, no CDP.
"""

import pytest

from trailblazer.agents.form_filler.stub import StubFormFiller
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.stub import StubScraper
from trailblazer.contracts import Control, Option, PageDescription, Walk
from trailblazer.loop.orchestrator import Loop
from tests.agents.frontier.frontier_test_data import (
    PAGE_1_BUSINESS_INFO,
    PAGE_SIMPLE,
    PAGE_SIMPLE_2,
    PIE_DISCOVERABLE,
    PIE_REVEALED_LOCATION_COUNT,
    REVEALED_PRONOUNS,
)

JOB = "job_e2e"
GENDER_OPTIONS = [
    Option(label="Male", locator="#gender-male"),
    Option(label="Female", locator="#gender-female"),
]


@pytest.fixture
def pages():
    return [PageDescription(**PAGE_SIMPLE), PageDescription(**PAGE_SIMPLE_2)]


def build(pages, reveals=None, discoverable=None):
    scraper = StubScraper(pages, reveals=reveals)
    frontier = FrontierAgent()
    filler = StubFormFiller(discoverable=discoverable or {"q_gender": GENDER_OPTIONS})
    return Loop(scraper, frontier, filler), frontier


class TestFullWalk:
    def test_observed_log_walks_every_option_in_place(self, pages):
        """
        What actually happened on the page, branches interleaved. Efficient to
        explore, but NOT replayable — see the next test for what ships.
        """
        loop, frontier = build(pages)
        loop.fill_form(JOB, pages[0])

        assert [
            (s.action, s.fieldId or s.locator, s.option or s.value)
            for s in frontier.walk_log
        ] == [
            ("type", "q_name", "Test Value"),
            ("choose", "q_gender", "Male"),  # filler discovered the dropdown, took [0]
            ("choose", "q_gender", "Female"),  # and Frontier came back for the rest
            ("choose", "q_consent", "Yes"),  # options came from the PD
            ("choose", "q_consent", "Maybe"),
            ("type", "q_email", "test@example.com"),
            ("click", 'button:has-text("Next")', None),
            ("type", "q_phone", "5551234567"),
            ("type", "q_start", "01/01/2026"),
        ]

    def test_publishes_one_replayable_path_per_branch(self, pages):
        """
        Two choosers with two options each -> 3 paths: a baseline plus one
        variant per extra option. Not 4 — MASTER.md forbids walking
        combinations of independent gates.
        """
        loop, _ = build(pages)
        walk = loop.fill_form(JOB, pages[0])

        assert isinstance(walk, Walk)
        assert [p.choices for p in walk.paths] == [
            {"q_gender": "Male", "q_consent": "Yes"},
            {"q_gender": "Female", "q_consent": "Yes"},
            {"q_gender": "Male", "q_consent": "Maybe"},
        ]

        # The baseline path, in full. Note it picks exactly ONE Gender option
        # and ONE Consent option — the thing a single interleaved slice got wrong.
        assert [
            (s.action, s.fieldId or s.locator, s.option or s.value)
            for s in walk.paths[0].steps
        ] == [
            ("type", "q_name", "Test Value"),
            ("choose", "q_gender", "Male"),
            ("choose", "q_consent", "Yes"),
            ("type", "q_email", "test@example.com"),
            ("click", 'button:has-text("Next")', None),
            ("type", "q_phone", "5551234567"),
            ("type", "q_start", "01/01/2026"),
        ]

    def test_no_path_selects_two_options_of_one_chooser(self, pages):
        """The defect this design fixes: a script that clicks Male then Female."""
        loop, _ = build(pages)
        walk = loop.fill_form(JOB, pages[0])

        for path in walk.paths:
            chosen = [s.fieldId for s in path.steps if s.action == "choose"]
            assert len(chosen) == len(set(chosen)), f"{path.choices} double-clicks a chooser"

    def test_every_path_shares_the_unconditional_steps(self, pages):
        """
        Nothing on this page is conditionally revealed, so all three paths carry
        the same non-choice steps — they differ only in which options they pin.
        """
        loop, _ = build(pages)
        walk = loop.fill_form(JOB, pages[0])

        plain_steps = [
            tuple(
                (s.action, s.fieldId, s.value)
                for s in path.steps
                if s.action != "choose"
            )
            for path in walk.paths
        ]
        assert len(set(plain_steps)) == 1

    def test_every_control_on_every_page_is_explored(self, pages):
        loop, frontier = build(pages)
        loop.fill_form(JOB, pages[0])

        assert {c.fieldId for c in frontier.board.controls} == {
            "q_name",
            "q_gender",
            "q_consent",
            "q_email",
            "q_phone",
            "q_start",
        }
        assert all(c.explored for c in frontier.board.controls)
        assert frontier.board.status == "complete"

    def test_discovered_chooser_ends_up_fully_walked(self, pages):
        loop, frontier = build(pages)
        loop.fill_form(JOB, pages[0])

        gender = next(c for c in frontier.board.controls if c.fieldId == "q_gender")
        assert [o.label for o in gender.walked] == ["Male", "Female"]
        assert gender.pending == []

    def test_reaches_the_second_page(self, pages):
        loop, frontier = build(pages)
        loop.fill_form(JOB, pages[0])

        assert frontier.board.currentStageId == "simple_page_2"

    def test_paths_span_both_pages(self, pages):
        loop, _ = build(pages)
        walk = loop.fill_form(JOB, pages[0])

        for path in walk.paths:
            fields = [s.fieldId for s in path.steps]
            assert "q_name" in fields  # page 1
            assert "q_phone" in fields  # page 2


class TestRevealedControls:
    """
    REVEALED_PRONOUNS declares revealedBy {fieldId: q_gender, equals: Female},
    so the reveal is scripted on Female to match.
    """

    REVEALS = {("q_gender", "Female"): [Control(**REVEALED_PRONOUNS)]}

    def test_field_revealed_mid_walk_is_explored(self, pages):
        loop, frontier = build(pages, reveals=self.REVEALS)
        loop.fill_form(JOB, pages[0])

        assert any(s.fieldId == "q_pronouns" for s in frontier.walk_log)
        pronouns = next(c for c in frontier.board.controls if c.fieldId == "q_pronouns")
        assert pronouns.explored is True

    def test_revealed_field_is_explored_while_its_option_is_still_set(self, pages):
        """
        The field only exists while Gender == Female. If Frontier waited until
        after it switched options, the field would be gone and the fill would
        fail — so it must come immediately after the Female choice.
        """
        loop, frontier = build(pages, reveals=self.REVEALS)
        loop.fill_form(JOB, pages[0])

        observed = [(s.fieldId, s.option) for s in frontier.walk_log]
        female = observed.index(("q_gender", "Female"))
        pronouns = [i for i, (f, _) in enumerate(observed) if f == "q_pronouns"][0]

        assert pronouns == female + 1

    def test_revealed_field_lands_only_on_its_own_branch(self, pages):
        """
        q_pronouns belongs to the Female path. Putting it on the Male path would
        compile a script that fills a field which isn't on the page.
        """
        loop, _ = build(pages, reveals=self.REVEALS)
        walk = loop.fill_form(JOB, pages[0])

        for path in walk.paths:
            has_pronouns = any(s.fieldId == "q_pronouns" for s in path.steps)
            assert has_pronouns == (path.choices["q_gender"] == "Female"), (
                f"{path.choices} has_pronouns={has_pronouns}"
            )

    def test_revealed_field_precedes_the_next_click_on_its_path(self, pages):
        loop, _ = build(pages, reveals=self.REVEALS)
        walk = loop.fill_form(JOB, pages[0])

        female = next(p for p in walk.paths if p.choices["q_gender"] == "Female")
        fields = [s.fieldId for s in female.steps]
        click_at = next(i for i, s in enumerate(female.steps) if s.action == "click")

        assert fields.index("q_pronouns") < click_at


class TestFailureHandling:
    def test_blocked_page_stops_without_a_walk_slice(self, pages):
        pages[0].blockers = ["Session expired"]
        loop, frontier = build(pages)

        assert loop.fill_form(JOB, pages[0]).paths == []
        assert frontier.board.status == "blocked"

    def test_failed_fill_stops_the_walk(self, pages):
        class BrokenFiller(StubFormFiller):
            def execute(self, job, stage_id, assignment):
                report = super().execute(job, stage_id, assignment)
                if getattr(assignment, "fieldId", None) == "q_gender":
                    report.ok = False
                    report.errorClass = "not_found"
                return report

        loop = Loop(StubScraper(pages), FrontierAgent(), BrokenFiller())

        # Stops mid-page rather than looping or publishing a partial walk.
        assert loop.fill_form(JOB, pages[0]).paths == []


PIE_OPTIONS = {
    field_id: [Option(**o) for o in options]
    for field_id, options in PIE_DISCOVERABLE.items()
}


def build_pie(reveals=None):
    """The real Pie page, end to end, with nothing hand-fed."""
    page = PageDescription(**PAGE_1_BUSINESS_INFO)
    loop = Loop(
        StubScraper([page], reveals=reveals),
        FrontierAgent(),
        StubFormFiller(discoverable=PIE_OPTIONS),
        recursion_limit=120,
    )
    return loop, page


class TestMasterPageDescription:
    def test_walks_every_control_on_the_real_page(self):
        loop, page = build_pie()
        loop.fill_form(JOB, page)
        frontier = loop.frontier

        assert len(frontier.board.controls) == 9
        assert all(c.explored for c in frontier.board.controls)

    def test_discovered_dropdowns_walked_in_full(self):
        loop, page = build_pie()
        loop.fill_form(JOB, page)

        entity = next(c for c in loop.frontier.board.controls if c.fieldId == "q_006")
        assert [o.label for o in entity.walked] == [
            "Limited Liability Company",
            "Corporation",
            "Sole Proprietor",
        ]

        agency = next(c for c in loop.frontier.board.controls if c.fieldId == "q_001")
        assert [o.label for o in agency.walked] == ["Pie Direct", "Pie Partner Program"]

    def test_split_control_uses_each_options_own_locator(self):
        """
        q_009's control locator IS the "Yes" option's locator. Selecting "No"
        through the control locator would silently re-click "Yes" — so the walk
        must show the option locators, distinct from each other.
        """
        loop, page = build_pie()
        loop.fill_form(JOB, page)

        q_009 = [
            (s.option, s.locator)
            for s in loop.frontier.walk_log
            if s.action == "choose" and s.fieldId == "q_009"
        ]
        assert q_009 == [
            ("Yes", 'internal:label="Yes"i'),
            ("No", 'internal:label="No"i'),
        ]

    def test_observed_log_of_the_whole_page(self):
        """In-place exploration order: every option of every chooser."""
        loop, page = build_pie()
        loop.fill_form(JOB, page)

        assert [
            (s.action, s.fieldId, s.option or s.value) for s in loop.frontier.walk_log
        ] == [
            ("choose", "q_001", "Pie Direct"),
            ("choose", "q_001", "Pie Partner Program"),
            ("type", "q_002", "01/01/2026"),
            ("type", "q_003", "10001"),
            ("type", "q_004", "Test Value"),
            ("type", "q_005", "Test Value"),
            ("choose", "q_006", "Limited Liability Company"),
            ("choose", "q_006", "Corporation"),
            ("choose", "q_006", "Sole Proprietor"),
            ("type", "q_007", "12-3456789"),
            ("type", "q_008", "10000"),
            ("choose", "q_009", "Yes"),
            ("choose", "q_009", "No"),
        ]

    def test_five_paths_not_twelve(self):
        """
        Choosers on this page: q_001 (2 options), q_006 (3), q_009 (2).
        Combinations would be 2 x 3 x 2 = 12. MASTER.md forbids that, so it's
        a baseline plus one variant per extra option: 1 + 1 + 2 + 1 = 5.
        """
        loop, page = build_pie()
        walk = loop.fill_form(JOB, page)

        assert len(walk.paths) == 5
        assert [p.choices for p in walk.paths] == [
            {"q_001": "Pie Direct", "q_006": "Limited Liability Company", "q_009": "Yes"},
            {"q_001": "Pie Partner Program", "q_006": "Limited Liability Company", "q_009": "Yes"},
            {"q_001": "Pie Direct", "q_006": "Corporation", "q_009": "Yes"},
            {"q_001": "Pie Direct", "q_006": "Sole Proprietor", "q_009": "Yes"},
            {"q_001": "Pie Direct", "q_006": "Limited Liability Company", "q_009": "No"},
        ]

    def test_every_path_is_a_complete_form_fill(self):
        """
        Each path must fill all six plain fields and pick exactly one option per
        chooser — i.e. be a runnable script on its own.
        """
        loop, page = build_pie()
        walk = loop.fill_form(JOB, page)

        for path in walk.paths:
            typed = {s.fieldId for s in path.steps if s.action == "type"}
            chosen = [s.fieldId for s in path.steps if s.action == "choose"]

            assert typed == {"q_002", "q_003", "q_004", "q_005", "q_007", "q_008"}
            assert chosen == ["q_001", "q_006", "q_009"]
            assert all(s.locator for s in path.steps)

    def test_field_revealed_by_a_real_answer_lands_on_that_branch_only(self):
        """
        Answering "multiple locations?" = Yes reveals a count field. It belongs
        on the Yes paths and must not appear on the No path, where the field
        doesn't exist.
        """
        loop, page = build_pie(
            reveals={("q_009", "Yes"): [Control(**PIE_REVEALED_LOCATION_COUNT)]}
        )
        walk = loop.fill_form(JOB, page)

        count = next(c for c in loop.frontier.board.controls if c.fieldId == "q_012")
        assert count.explored is True
        assert count.revealedBy.fieldId == "q_009"

        for path in walk.paths:
            has_count = any(s.fieldId == "q_012" for s in path.steps)
            assert has_count == (path.choices["q_009"] == "Yes"), (
                f"{path.choices} has_count={has_count}"
            )

    def test_next_that_does_not_navigate_settles_the_walk(self):
        """
        The real page has a Next button, but we only scripted one page. Frontier
        clicks Next once, finds itself back on the same stage, and publishes the
        walk instead of clicking Next forever.
        """
        loop, page = build_pie()
        walk = loop.fill_form(JOB, page)

        assert loop.frontier.board.status == "slice_stable"
        # The Next click never landed, so it's in no path.
        for path in walk.paths:
            assert not any(s.action == "click" for s in path.steps)
