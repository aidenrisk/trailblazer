"""
The three-field walkthrough, one test per step: Name, Gender, Email.

Written out longhand rather than as a loop, because the point of this file is to
be readable as a specification — each test names the step it pins, and the
assertions are the contract.

The setup: Scraper reports all three controls with `options: null`. It has no
idea Gender is a dropdown, and `candidateGates` is empty, so nothing tells
Frontier in advance that Gender branches. FormFiller finds out by trying to fill
it. The page has no Next button, so a fully-explored page ends the walk.
"""

import pytest

from trailblazer.agents.form_filler.stub import StubFormFiller
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.stub import StubScraper
from trailblazer.contracts import Diff, FillReport, Option, PageDescription, Walk
from trailblazer.loop.orchestrator import Loop
from tests.agents.frontier.frontier_test_data import (
    GENDER_OPTIONS,
    PAGE_NAME_GENDER_EMAIL,
)

JOB = "job_basic"
MALE, FEMALE = (Option(**o) for o in GENDER_OPTIONS)


@pytest.fixture
def agent():
    return FrontierAgent()


@pytest.fixture
def page():
    return PageDescription(**PAGE_NAME_GENDER_EMAIL)


def plain(field_id):
    """What FormFiller reports after filling an ordinary text box."""
    return FillReport(ok=True, fieldId=field_id, discoveredOptions=None)


def discovered(field_id, options, chosen):
    """What FormFiller reports when the control turned out to be a dropdown."""
    return FillReport(
        ok=True, fieldId=field_id, discoveredOptions=options, chosenOption=chosen
    )


def chose(field_id, label):
    """What FormFiller reports after an explicit set_option."""
    return FillReport(ok=True, fieldId=field_id, chosenOption=label)


SETTLED = Diff(polarity="-ve")


class TestStepByStep:
    """Each test walks from the start, so every step reads independently."""

    def test_step_1_frontier_starts_with_name(self, agent, page):
        a = agent.on_page(JOB, page)

        assert a.type == "fill_field"
        assert a.fieldId == "q_name"
        assert a.locator == "#name"
        assert a.value == "Test Value"

    def test_step_2_name_done_move_to_gender(self, agent, page):
        agent.on_page(JOB, page)
        a = agent.on_page(JOB, page, SETTLED, plain("q_name"))

        # Frontier has no reason to think Gender is special — same assignment
        # type it used for Name.
        assert a.type == "fill_field"
        assert a.fieldId == "q_gender"
        assert a.locator == "#gender"

        name = next(c for c in agent.board.controls if c.fieldId == "q_name")
        assert name.explored is True

    def test_step_3_filler_reports_gender_is_a_dropdown(self, agent, page):
        agent.on_page(JOB, page)
        agent.on_page(JOB, page, SETTLED, plain("q_name"))
        agent.on_page(JOB, page, SETTLED, discovered("q_gender", [MALE, FEMALE], "Male"))

        gender = next(c for c in agent.board.controls if c.fieldId == "q_gender")
        assert [o.label for o in gender.options] == ["Male", "Female"]
        assert [o.label for o in gender.walked] == ["Male"]
        assert [o.label for o in gender.pending] == ["Female"]
        # The whole point: one option down, one to go, so NOT done.
        assert gender.explored is False

    def test_step_4_frontier_returns_to_gender_not_email(self, agent, page):
        agent.on_page(JOB, page)
        agent.on_page(JOB, page, SETTLED, plain("q_name"))
        a = agent.on_page(
            JOB, page, SETTLED, discovered("q_gender", [MALE, FEMALE], "Male")
        )

        # Email is next in page order, but Gender isn't finished, so Gender wins.
        assert a.type == "set_option"
        assert a.fieldId == "q_gender"
        assert a.option == "Female"
        assert a.locator == "#gender-female"  # the option's locator
        assert a.controlLocator == "#gender"  # the control's

    def test_step_5_gender_explored_once_both_options_walked(self, agent, page):
        agent.on_page(JOB, page)
        agent.on_page(JOB, page, SETTLED, plain("q_name"))
        agent.on_page(JOB, page, SETTLED, discovered("q_gender", [MALE, FEMALE], "Male"))
        a = agent.on_page(JOB, page, SETTLED, chose("q_gender", "Female"))

        gender = next(c for c in agent.board.controls if c.fieldId == "q_gender")
        assert gender.pending == []
        assert [o.label for o in gender.walked] == ["Male", "Female"]
        assert gender.explored is True

        # Only now does Email come up.
        assert a.type == "fill_field"
        assert a.fieldId == "q_email"
        # Label-aware value, so the fill would actually pass validation.
        assert a.value == "test@example.com"

    def test_step_6_page_done_publishes_one_path_per_branch(self, agent, page):
        agent.on_page(JOB, page)
        agent.on_page(JOB, page, SETTLED, plain("q_name"))
        agent.on_page(JOB, page, SETTLED, discovered("q_gender", [MALE, FEMALE], "Male"))
        agent.on_page(JOB, page, SETTLED, chose("q_gender", "Female"))
        outcome = agent.on_page(JOB, page, SETTLED, plain("q_email"))

        # No Next button and nothing left to explore -> the walk is over.
        assert isinstance(outcome, Walk)
        assert agent.board.status == "complete"
        assert all(c.explored for c in agent.board.controls)

        # Gender has two options, so the form has two paths through it.
        assert [p.choices for p in outcome.paths] == [
            {"q_gender": "Male"},
            {"q_gender": "Female"},
        ]


class TestTheWholeSequence:
    def test_assignment_order(self, agent, page):
        """Every assignment Frontier emits, in order."""
        emitted = []
        report = None

        for _ in range(10):
            action = agent.on_page(JOB, page, SETTLED, report)
            if isinstance(action, Walk):
                break

            emitted.append(action)
            if action.type == "set_option":
                report = chose(action.fieldId, action.option)
            elif action.fieldId == "q_gender":
                report = discovered("q_gender", [MALE, FEMALE], "Male")
            else:
                report = plain(action.fieldId)

        assert [
            (a.type, a.fieldId, getattr(a, "option", None) or a.value) for a in emitted
        ] == [
            ("fill_field", "q_name", "Test Value"),
            ("fill_field", "q_gender", "Test Value"),
            ("set_option", "q_gender", "Female"),
            ("fill_field", "q_email", "test@example.com"),
        ]

    def test_each_path_is_independently_replayable(self, agent, page):
        """
        End to end through Loop and the stubs — no hand-fed reports.

        Two paths, one per Gender option. Each is a complete script on its own:
        every step has a locator plus either a value or an option, and no path
        contains two options of the same chooser (which would make the first
        click pointless).
        """
        loop = Loop(
            StubScraper([page]),
            agent,
            StubFormFiller(discoverable={"q_gender": [MALE, FEMALE]}),
        )
        walk = loop.fill_form(JOB, page)

        assert len(walk.paths) == 2

        male, female = walk.paths
        assert male.choices == {"q_gender": "Male"}
        assert [
            (s.action, s.fieldId, s.locator, s.option or s.value) for s in male.steps
        ] == [
            ("type", "q_name", "#name", "Test Value"),
            ("choose", "q_gender", "#gender-male", "Male"),
            ("type", "q_email", "#email", "test@example.com"),
        ]

        assert female.choices == {"q_gender": "Female"}
        assert [
            (s.action, s.fieldId, s.locator, s.option or s.value) for s in female.steps
        ] == [
            ("type", "q_name", "#name", "Test Value"),
            ("choose", "q_gender", "#gender-female", "Female"),
            ("type", "q_email", "#email", "test@example.com"),
        ]

        for path in walk.paths:
            assert all(s.locator for s in path.steps)
            assert all(s.value or s.option for s in path.steps)
            assert [s.fieldId for s in path.steps].count("q_gender") == 1

    def test_gender_is_visited_twice_but_each_path_touches_it_once(self, agent, page):
        """
        Three controls, four assignments: the extra one is Gender's second
        option. That's the exploration. Each replayable path still touches
        Gender exactly once — that's the reconstruction.
        """
        loop = Loop(
            StubScraper([page]),
            agent,
            StubFormFiller(discoverable={"q_gender": [MALE, FEMALE]}),
        )
        walk = loop.fill_form(JOB, page)

        observed = [s.fieldId for s in agent.walk_log]
        assert observed.count("q_name") == 1
        assert observed.count("q_gender") == 2
        assert observed.count("q_email") == 1

        for path in walk.paths:
            touched = [s.fieldId for s in path.steps]
            assert touched == ["q_name", "q_gender", "q_email"]
