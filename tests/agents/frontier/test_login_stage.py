"""
Frontier on a login stage: fill credentials by key, pick email once, never explore.

Login is page 1 of the same chain. These run the real Frontier through the Loop
against the stub Scraper and stub FormFiller, exactly like the form walks in
tests/loop, so what is asserted is the protocol: which Assignments Frontier
issues on a login_* page, and what the published Walk looks like afterwards.
Fully offline: no browser, no model, no database.
"""

import pytest

from trailblazer.agents.form_filler.stub import StubFormFiller
from trailblazer.agents.frontier.board import FrontierBoardState
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.stub import StubScraper
from trailblazer.contracts import (
    LOGIN_EMAIL,
    LOGIN_OTP,
    LOGIN_PASSWORD,
    ControlState,
    FillFieldAssignment,
    PageDescription,
    SetOptionAssignment,
    SimpleAssignment,
    Walk,
)
from trailblazer.loop.orchestrator import Loop
from tests.agents.frontier.frontier_test_data import (
    FORM_AFTER_LOGIN,
    LOGIN_CHANNEL_PAGE,
    LOGIN_OTP_PAGE,
    LOGIN_PAGE,
    LOGIN_SECOND_HOST,
    LOGIN_STEP_PASSWORD,
    LOGIN_STEP_USERNAME,
)

JOB = "job_login"


def pages(*dicts) -> list[PageDescription]:
    return [PageDescription(**d) for d in dicts]


def walk_through(*dicts) -> tuple[Walk, FrontierAgent, StubFormFiller]:
    """Drive the whole chain over the scripted pages and return what came out."""
    scripted = pages(*dicts)
    frontier = FrontierAgent()
    filler = RecordingFiller()
    loop = Loop(StubScraper(scripted), frontier, filler)
    walk = loop.fill_form(JOB, scripted[0])
    return walk, frontier, filler


class RecordingFiller(StubFormFiller):
    """The stub filler, plus a record of every Assignment it was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.assignments = []

    def execute(self, job, stage_id, assignment):
        self.assignments.append(assignment)
        return super().execute(job, stage_id, assignment)


def brief(steps) -> list[tuple]:
    """(action, fieldId-or-locator, credentialKey-or-option-or-value) for readable asserts."""
    return [(s.action, s.fieldId or s.locator, s.credentialKey or s.option or s.value) for s in steps]


# --------------------------------------------------------------------------- #
# The happy path: sign in, pick email, clear the code, reach the form.
# --------------------------------------------------------------------------- #


class TestLoginThenForm:
    @pytest.fixture
    def outcome(self):
        return walk_through(LOGIN_PAGE, LOGIN_CHANNEL_PAGE, LOGIN_OTP_PAGE, FORM_AFTER_LOGIN)

    def test_credentials_are_filled_by_key_and_the_login_prefix_is_split_out(self, outcome):
        walk, _, _ = outcome

        assert brief(walk.login) == [
            ("type", "q_user", LOGIN_EMAIL),
            ("type", "q_pass", LOGIN_PASSWORD),
            ("click", 'button:has-text("Sign in")', None),
            ("choose", "q_channel", "Email"),
            ("click", 'button:has-text("Send code")', None),
            ("type", "q_code", LOGIN_OTP),
            ("click", 'button:has-text("Verify")', None),
        ]

    def test_no_secret_and_no_synthetic_value_ever_touches_a_credential(self, outcome):
        walk, _, filler = outcome

        credential_fills = [
            a for a in filler.assignments
            if isinstance(a, FillFieldAssignment) and a.credentialKey is not None
        ]
        assert [a.credentialKey for a in credential_fills] == [LOGIN_EMAIL, LOGIN_PASSWORD, LOGIN_OTP]
        assert all(a.value is None for a in credential_fills)
        assert all(s.value is None for s in walk.login if s.credentialKey)

    def test_nothing_else_on_a_login_page_is_touched(self, outcome):
        """The toggle, the resend chooser: left as the portal rendered them."""
        _, _, filler = outcome

        touched = {getattr(a, "fieldId", None) for a in filler.assignments}
        assert "q_remember" not in touched
        assert "q_resend" not in touched

    def test_the_email_channel_is_chosen_once_and_sms_never(self, outcome):
        _, frontier, filler = outcome

        chosen = [a for a in filler.assignments if isinstance(a, SetOptionAssignment)]
        login_choices = [a for a in chosen if a.fieldId == "q_channel"]
        assert [a.option for a in login_choices] == ["Email"]
        assert login_choices[0].locator == "#channel-email"

        channel = next(c for c in frontier.board.controls if c.fieldId == "q_channel")
        assert channel.explored and channel.pending == []
        assert [o.label for o in channel.walked] == ["Email"]

    def test_form_paths_carry_only_form_steps_and_the_login_does_not_multiply_them(self, outcome):
        """One two-option chooser on the form -> two paths, not four."""
        walk, _, _ = outcome

        assert [p.choices for p in walk.paths] == [{"q_entity": "LLC"}, {"q_entity": "Corporation"}]
        for path in walk.paths:
            assert not any(s.credentialKey for s in path.steps)
            assert brief(path.steps)[0] == ("type", "q_name", "Test Value")

    def test_the_observed_log_still_shows_everything_in_order(self, outcome):
        """walk_log is the unsplit record; login comes first because it happened first."""
        _, frontier, _ = outcome

        assert brief(frontier.walk_log)[:3] == [
            ("type", "q_user", LOGIN_EMAIL),
            ("type", "q_pass", LOGIN_PASSWORD),
            ("click", 'button:has-text("Sign in")', None),
        ]
        assert frontier.board.status == "complete"


# --------------------------------------------------------------------------- #
# Rejection: the same login page comes back after Sign in.
# --------------------------------------------------------------------------- #


class TestRejectedLogin:
    def test_stops_with_auth_instead_of_retrying_or_publishing(self):
        # Only the login page is scripted, so Next leaves the stub exactly where it was.
        walk, frontier, filler = walk_through(LOGIN_PAGE)

        assert walk == Walk()  # nothing published: a login that never landed is not a prefix
        assert frontier.board.status == "blocked"
        clicks = [a for a in filler.assignments if isinstance(a, SimpleAssignment)]
        assert [a.type for a in clicks] == ["next"]  # one attempt, no retry

    def test_the_stop_names_the_reason(self):
        scripted = pages(LOGIN_PAGE)
        frontier = FrontierAgent()
        page = scripted[0]

        first = frontier.on_page(JOB, page)
        assert isinstance(first, FillFieldAssignment) and first.credentialKey == LOGIN_EMAIL
        frontier.on_page(JOB, page, fill_report=StubFormFiller().execute(JOB, page.stageId, first))
        # Password filled, then Sign in.
        pw = frontier.on_page(JOB, page, fill_report=None)
        # Walk the graph by hand until Frontier asks to advance.
        while not (isinstance(pw, SimpleAssignment) and pw.type == "next"):
            pw = frontier.on_page(JOB, page, fill_report=StubFormFiller().execute(JOB, page.stageId, pw))
        # The click "happened" and the page is unchanged.
        after = frontier.on_page(JOB, page, fill_report=StubFormFiller().execute(JOB, page.stageId, pw))

        assert after == SimpleAssignment(type="stop", reason="auth")

    def test_the_unlanded_sign_in_click_is_dropped_from_the_log(self):
        _, frontier, _ = walk_through(LOGIN_PAGE)

        assert [s.action for s in frontier.walk_log] == ["type", "type"]


# --------------------------------------------------------------------------- #
# Progress under the same stage name is not a rejection.
# --------------------------------------------------------------------------- #


class TestTwoStepSignInOnOneStage:
    def test_a_new_credential_control_means_continue_not_rejected(self):
        walk, frontier, _ = walk_through(LOGIN_STEP_USERNAME, LOGIN_STEP_PASSWORD, FORM_AFTER_LOGIN)

        assert brief(walk.login) == [
            ("type", "q_user_a", LOGIN_EMAIL),
            ("click", 'button:has-text("Continue")', None),
            ("type", "q_pass_a", LOGIN_PASSWORD),
            ("click", 'button:has-text("Continue")', None),
        ]
        assert frontier.board.status == "complete"


class TestTwoHosts:
    def test_a_second_sign_in_on_another_host_joins_the_same_prefix(self):
        walk, _, _ = walk_through(LOGIN_PAGE, LOGIN_SECOND_HOST, FORM_AFTER_LOGIN)

        assert brief(walk.login) == [
            ("type", "q_user", LOGIN_EMAIL),
            ("type", "q_pass", LOGIN_PASSWORD),
            ("click", 'button:has-text("Sign in")', None),
            ("type", "q_user_b", LOGIN_EMAIL),
            ("type", "q_pass_b", LOGIN_PASSWORD),
            ("click", 'button:has-text("Log in")', None),
        ]


# --------------------------------------------------------------------------- #
# Channel chooser without an email option: leave it, move on.
# --------------------------------------------------------------------------- #


class TestChannelWithoutEmail:
    def test_no_option_is_picked_and_the_page_is_simply_advanced(self):
        sms_only = {
            **LOGIN_CHANNEL_PAGE,
            "controls": [
                {
                    **LOGIN_CHANNEL_PAGE["controls"][0],
                    "options": [
                        {"label": "Text message (SMS)", "locator": "#channel-sms"},
                        {"label": "Phone call", "locator": "#channel-voice"},
                    ],
                }
            ],
        }
        walk, _, filler = walk_through(LOGIN_PAGE, sms_only, FORM_AFTER_LOGIN)

        assert not any(isinstance(a, SetOptionAssignment) and a.fieldId == "q_channel" for a in filler.assignments)
        assert ("click", 'button:has-text("Send code")', None) in brief(walk.login)


# --------------------------------------------------------------------------- #
# Board-level rules, with no Loop around them.
# --------------------------------------------------------------------------- #


class TestBoardPolicy:
    def test_assignment_for_a_credential_never_asks_the_value_provider(self):
        def never(control):
            raise AssertionError(f"value_provider called for {control.fieldId}")

        board = FrontierBoardState(value_provider=never)
        board.sync_controls(PageDescription(**LOGIN_PAGE))

        for field_id, key in [("q_user", LOGIN_EMAIL), ("q_pass", LOGIN_PASSWORD)]:
            entry = next(c for c in board.board.controls if c.fieldId == field_id)
            a = board.assignment_for(entry)
            assert a == FillFieldAssignment(fieldId=field_id, locator=entry.locator, credentialKey=key)

    def test_non_credential_controls_on_a_login_page_arrive_already_explored(self):
        board = FrontierBoardState()
        board.sync_controls(PageDescription(**LOGIN_PAGE))
        board.sync_controls(PageDescription(**LOGIN_OTP_PAGE))

        by_id = {c.fieldId: c for c in board.board.controls}
        assert by_id["q_remember"].explored and by_id["q_remember"].pending == []
        assert by_id["q_resend"].explored and by_id["q_resend"].pending == []
        assert not by_id["q_user"].explored and not by_id["q_code"].explored

    def test_only_the_email_option_is_pending_on_a_channel_chooser(self):
        board = FrontierBoardState()
        board.sync_controls(PageDescription(**LOGIN_CHANNEL_PAGE))

        channel = board.board.controls[0]
        assert [o.label for o in channel.pending] == ["Email"]
        assert not channel.explored

    def test_options_learned_later_on_a_login_page_still_follow_the_policy(self):
        """Scraper may only see the channel options on a second look."""
        unknown = {**LOGIN_CHANNEL_PAGE, "controls": [{**LOGIN_CHANNEL_PAGE["controls"][0], "options": None}], "candidateGates": []}
        board = FrontierBoardState()
        board.sync_controls(PageDescription(**unknown))
        assert board.board.controls[0].explored  # unknown chooser on a login page: left alone

        board.sync_controls(PageDescription(**LOGIN_CHANNEL_PAGE))
        assert [o.label for o in board.board.controls[0].pending] == ["Email"]

    def test_login_signature_tracks_credential_controls_only(self):
        board = FrontierBoardState()
        page = PageDescription(**LOGIN_PAGE)
        board.note_advance_attempt(page.stageId, page.next, page)

        assert board.login_rejected(page)
        assert not board.login_can_advance_again(page)

        moved_on = PageDescription(**LOGIN_STEP_PASSWORD)  # same stage name, different credential
        assert not board.login_rejected(moved_on)
        assert board.login_can_advance_again(moved_on)

    def test_a_form_stage_is_never_read_as_a_login_rejection(self):
        board = FrontierBoardState()
        form = PageDescription(**FORM_AFTER_LOGIN)
        board.note_advance_attempt(form.stageId, 'button:has-text("Next")', form)

        assert not board.login_rejected(form)
        assert not board.login_can_advance_again(form)

    def test_control_state_carries_the_credential_kind(self):
        board = FrontierBoardState()
        board.sync_controls(PageDescription(**LOGIN_OTP_PAGE))

        code = next(c for c in board.board.controls if c.fieldId == "q_code")
        assert isinstance(code, ControlState) and code.credential == "otp"
