"""Login tests, against a local fixture and never a live portal.

What is under test is the journey's order and its grading: that a rejected
sign-in is reported as one, that an ambiguous field is refused rather than
guessed at, and that credentials do not reach the result.
"""

from pathlib import Path

import pytest

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.login import CarrierLogin, LoginError, registered, resolve_login
from trailblazer.agents.login.pie import PieLogin
from trailblazer.shared.dev_carrier_creds import CarrierCreds

FIXTURE_URL = (Path(__file__).parent / "fixtures" / "login.html").resolve().as_uri()

CREDS = CarrierCreds(login_url=FIXTURE_URL, username="agent@example.com", password="s3cret")


class FixtureLogin(CarrierLogin):
    """The local fixture's journey. Same shape as a real carrier's."""

    carrier_id = "fixture"
    username_selector = "#email"
    password_selector = "#password"
    submit_selector = "#signin"
    authenticated_selector = "#dashboard"
    mfa_selector = "#otp"


@pytest.fixture
def page():
    with BrowserSession(cdp_port=9340) as session:
        yield session.goto(FIXTURE_URL)


def test_a_correct_credential_signs_in(page) -> None:
    result = FixtureLogin(CREDS).sign_in(page)

    assert result.ok
    assert not result.mfa_required


def test_a_rejected_credential_is_not_reported_as_success(page) -> None:
    """The portal re-renders the same form, so nothing raises on its own."""
    bad = CarrierCreds(login_url=FIXTURE_URL, username="agent@example.com", password="wrong")

    result = FixtureLogin(bad).sign_in(page)

    assert not result.ok
    assert "#dashboard" in result.reason


def test_an_mfa_prompt_is_reported_rather_than_failed(page) -> None:
    """A one-time code is not in the credential store, so it is a distinct state."""
    mfa = CarrierCreds(login_url=FIXTURE_URL, username="mfa@example.com", password="s3cret")

    result = FixtureLogin(mfa).sign_in(page)

    assert result.mfa_required
    assert not result.ok


def test_the_result_never_carries_the_credential(page) -> None:
    result = FixtureLogin(CREDS).sign_in(page)

    assert "s3cret" not in repr(result)


def test_an_ambiguous_field_is_refused_not_guessed(page) -> None:
    """Typing a password into whichever box came first sends a credential nowhere."""

    class Ambiguous(FixtureLogin):
        carrier_id = "ambiguous"
        password_selector = "input"

    with pytest.raises(LoginError, match="exactly one is required"):
        Ambiguous(CREDS).sign_in(page)


def test_a_class_with_no_authenticated_selector_cannot_grade_itself(page) -> None:
    """Defaulting to True would walk a sign-in page as the application form."""

    class Ungraded(FixtureLogin):
        carrier_id = "ungraded"
        authenticated_selector = ""

    with pytest.raises(LoginError, match="authenticated_selector"):
        Ungraded(CREDS).sign_in(page)


def test_the_steps_are_recorded_with_placeholders_not_literals(page) -> None:
    """The replay script re-runs these, and the metadata artifact stores them."""
    result = FixtureLogin(CREDS).sign_in(page)

    assert [(s.action, s.selector, s.value) for s in result.steps] == [
        ("goto", "", FIXTURE_URL),
        ("fill", "#email", "$EMAIL"),
        ("fill", "#password", "$PASSWORD"),
        ("click", "#signin", ""),
    ]


def test_a_skipped_login_still_reports_the_journey(page) -> None:
    """The crawl's profile may hold a session; the replay's fresh browser cannot."""
    login = FixtureLogin(CREDS)
    login.sign_in(page)  # now signed in, so the next call short-circuits

    again = login.sign_in(page)

    assert again.ok and again.reason == "already signed in"
    assert [s.action for s in again.steps] == ["goto", "fill", "fill", "click"]
    assert [s.value for s in again.steps if s.action == "fill"] == ["$EMAIL", "$PASSWORD"]


def test_a_headed_run_can_wait_for_a_code_entered_by_hand(page) -> None:
    """The credentials were accepted; only the code is missing, and a human has it."""
    mfa = CarrierCreds(login_url=FIXTURE_URL, username="mfa@example.com", password="s3cret")
    login = FixtureLogin(mfa)
    assert login.sign_in(page).mfa_required

    # What a person entering the code in the window amounts to.
    page.evaluate("document.getElementById('dashboard').hidden = false")

    assert login.wait_for_manual_completion(page, timeout_s=10).ok


def test_waiting_for_a_code_gives_up_rather_than_hanging(page) -> None:
    mfa = CarrierCreds(login_url=FIXTURE_URL, username="mfa@example.com", password="s3cret")
    login = FixtureLogin(mfa)
    login.sign_in(page)

    result = login.wait_for_manual_completion(page, timeout_s=3)

    assert not result.ok and result.mfa_required
    # The replay still needs the journey, whether or not this run completed it.
    assert [s.action for s in result.steps] == ["goto", "fill", "fill", "click"]


def test_an_unknown_carrier_has_no_login_rather_than_the_base_journey() -> None:
    """The base class cannot tell a signed-in page from a rejected one."""
    with pytest.raises(KeyError, match="no login class registered"):
        resolve_login("not-a-carrier", CREDS)


def test_pie_is_registered_and_carries_measured_selectors() -> None:
    assert "pie" in registered()
    assert isinstance(resolve_login("pie", CREDS), PieLogin)
    # Taken from outputs/signin and outputs/dash, not invented.
    assert PieLogin.username_selector == "#emailAddress"
    assert PieLogin.authenticated_selector == '[data-testid="partnerNavSearch"]'
