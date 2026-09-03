"""Tests for the form filler, against the local fixture and never a live portal.

No API key is needed. `choose_value` is the filler's only LLM call and it is
patched wherever a test exercises a path through it -- what is under test is the
page interaction, the denylist, the retry and the report shape, none of which
depend on which value a model would have picked.

Every browser here is a separate `BrowserSession` on its own CDP port, matching
`test_scraper.py`, so no test can be affected by a page another test left dirty.
"""

from pathlib import Path

import pytest

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.browser.write_tools import LocatorError, resolve
from trailblazer.agents.form_filler import form_filler
from trailblazer.agents.form_filler.form_filler import _shape_of, fill_one
from trailblazer.agents.form_filler.safety import denial_reason
from trailblazer.contracts.assignment import Assignment
from trailblazer.observability.ledger import RunLedger
from trailblazer.shared.config import Settings

FIXTURE_URL = (Path(__file__).parent / "fixtures" / "fill.html").resolve().as_uri()

SETTINGS = Settings(carrier_url="http://localhost/x", carrier_username="agent@example.com",
                    carrier_password="s3cret-not-in-any-report")


@pytest.fixture
def page():
    """A fresh browser on the fixture. One per test: fills are not undone."""
    with BrowserSession(cdp_port=9330) as session:
        yield session.goto(FIXTURE_URL)


def _no_llm(monkeypatch, value: str = "123456789"):
    """Replace the value chooser with a fixed answer and record its calls."""
    calls: list[dict] = []

    def fake(label, locator, url, constraint_hint, error_text, settings):
        calls.append({"label": label, "error_text": error_text, "hint": constraint_hint})
        answer = value(len(calls)) if callable(value) else value
        return answer, 0.0012, False

    monkeypatch.setattr(form_filler, "choose_value", fake)
    return calls


# --------------------------------------------------------------------------- #
# fill
# --------------------------------------------------------------------------- #


def test_fill_types_the_assignment_value_and_reports_it(page) -> None:
    report = fill_one(
        page,
        Assignment(intent="fill", locator="#legalName", fieldId="q_001", value="Acme Roofing LLC"),
        SETTINGS,
    )

    assert report.ok
    assert report.valueUsed == "Acme Roofing LLC"
    assert report.retried is False
    assert report.blocked is None
    assert page.locator("#legalName").input_value() == "Acme Roofing LLC"


def test_fill_with_no_value_asks_for_one_and_reports_what_it_typed(page, monkeypatch) -> None:
    """The value is the filler's judgment; the assignment carries none."""
    calls = _no_llm(monkeypatch, "Bluebird Landscaping")

    report = fill_one(page, Assignment(intent="fill", locator="#legalName", fieldId="q_001"), SETTINGS)

    assert report.ok
    assert report.valueUsed == "Bluebird Landscaping"
    assert page.locator("#legalName").input_value() == "Bluebird Landscaping"
    # The model is given the page's own label, not the opaque fieldId.
    assert calls[0]["label"] == "Legal Business Name"


# --------------------------------------------------------------------------- #
# select and check
# --------------------------------------------------------------------------- #


def test_select_on_a_radio_clicks_the_option_locator(page) -> None:
    report = fill_one(
        page,
        Assignment(
            intent="select",
            locator="#priorClaimsGroup",
            fieldId="q_005",
            value="Yes",
            optionLocator="#priorClaimsYes",
        ),
        SETTINGS,
    )

    assert report.ok
    assert report.valueUsed == "Yes"
    assert page.locator("#priorClaimsYes").is_checked()
    assert not page.locator("#priorClaimsNo").is_checked()


def test_select_on_a_native_select_sets_it_by_label(page) -> None:
    report = fill_one(
        page,
        Assignment(
            intent="select",
            locator="#entityType",
            fieldId="q_004",
            value="Limited Liability Company",
        ),
        SETTINGS,
    )

    assert report.ok
    assert report.valueUsed == "Limited Liability Company"
    assert page.locator("#entityType").input_value() == "llc"


def test_select_by_label_falls_back_to_the_option_value(page) -> None:
    """A PageDescription records labels, but a caller holding the value still works."""
    report = fill_one(
        page, Assignment(intent="select", locator="#entityType", fieldId="q_004", value="corp"), SETTINGS
    )

    assert report.ok
    assert page.locator("#entityType").input_value() == "corp"


def test_select_with_an_unknown_label_is_blocked_not_raised(page) -> None:
    report = fill_one(
        page,
        Assignment(intent="select", locator="#entityType", fieldId="q_004", value="Partnership"),
        SETTINGS,
    )

    assert report.ok is False
    assert "Partnership" in report.blocked["whatYouTried"]


def test_check_clicks_the_control_and_reports_the_state(page) -> None:
    report = fill_one(
        page, Assignment(intent="check", locator="#priorCoverage", fieldId="q_006"), SETTINGS
    )

    assert report.ok
    assert report.valueUsed == "true"
    assert page.locator("#priorCoverage").is_checked()


# --------------------------------------------------------------------------- #
# expand
# --------------------------------------------------------------------------- #


def test_expand_reads_the_options_and_leaves_the_widget_closed(page) -> None:
    """The whole reason expand exists: the listbox does not exist until opened."""
    assert page.locator('#agencyList [role="option"]').count() == 0

    report = fill_one(page, Assignment(intent="expand", locator="#agency", fieldId="q_007"), SETTINGS)

    assert report.ok
    assert report.optionsRevealed == ["Direct", "Broker Network", "Affinity Partner", "Wholesale"]
    assert page.locator("#agency").get_attribute("aria-expanded") == "false"
    assert page.locator('#agencyList [role="option"]').count() == 0


def test_expand_does_not_select_anything(page) -> None:
    """The control's displayed value is unchanged: no option was committed."""
    before = page.locator("#agency").inner_text()

    fill_one(page, Assignment(intent="expand", locator="#agency", fieldId="q_007"), SETTINGS)

    assert page.locator("#agency").inner_text() == before == "Choose an agency"


def test_expand_refuses_a_control_that_is_not_a_disclosure(page) -> None:
    """A plain button has no aria-expanded and no listbox relationship."""
    report = fill_one(page, Assignment(intent="expand", locator="#next-btn", fieldId=None), SETTINGS)

    assert report.ok is False
    assert "not a disclosure" in report.blocked["whatYouTried"]
    assert report.optionsRevealed is None


def test_expand_reads_a_native_selects_own_options(page) -> None:
    """A native select never mounts a listbox; its <option> elements are read."""
    report = fill_one(page, Assignment(intent="expand", locator="#entityType", fieldId="q_004"), SETTINGS)

    assert report.ok
    assert "Limited Liability Company" in report.optionsRevealed
    assert page.locator("#entityType").input_value() == ""


# --------------------------------------------------------------------------- #
# The retry the filler owns
# --------------------------------------------------------------------------- #


def test_a_rejected_fill_is_retried_and_the_constraint_is_reported(page, monkeypatch) -> None:
    """The nine-digit requirement is stated nowhere; only the shape that cleared it is."""
    _no_llm(monkeypatch, lambda n: "12-3456" if n == 1 else "123456789")

    report = fill_one(page, Assignment(intent="fill", locator="#fein", fieldId="q_002"), SETTINGS)

    assert report.ok
    assert report.retried is True
    assert report.valueUsed == "123456789"
    assert report.constraint == {
        "unit": "",
        "format": "999999999",
        "hint": "Please enter the FEIN",
    }
    assert page.locator("#fein").get_attribute("aria-invalid") == "false"


def test_the_correction_call_is_given_the_pages_error_text(page, monkeypatch) -> None:
    calls = _no_llm(monkeypatch, lambda n: "12-3456" if n == 1 else "123456789")

    fill_one(page, Assignment(intent="fill", locator="#fein", fieldId="q_002"), SETTINGS)

    assert calls[0]["error_text"] is None
    assert calls[1]["error_text"] == "Please enter the FEIN"


def test_an_accepted_fill_reports_no_constraint_and_no_retry(page, monkeypatch) -> None:
    _no_llm(monkeypatch, "123456789")

    report = fill_one(page, Assignment(intent="fill", locator="#fein", fieldId="q_002"), SETTINGS)

    assert report.ok
    assert report.retried is False
    assert report.constraint is None


def test_a_field_still_rejected_after_two_retries_is_blocked(page, monkeypatch) -> None:
    calls = _no_llm(monkeypatch, "anything")

    report = fill_one(page, Assignment(intent="fill", locator="#impossible", fieldId="q_003"), SETTINGS)

    assert report.ok is False
    assert "still rejected after 2 corrections" in report.blocked["whatYouTried"]
    # One first choice plus exactly two corrections; the budget is not exceeded.
    assert len(calls) == 3


# --------------------------------------------------------------------------- #
# The denylist. Non-negotiable, checked in Python, before dispatch.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "locator",
    ["#pay-btn", "#bind-btn"],
    ids=["purchase-policy", "bind-coverage"],
)
def test_a_denylisted_button_is_refused_and_never_clicked(page, locator: str) -> None:
    """The click must not reach the page: a bind is irreversible and external."""
    page.evaluate("() => { window.__clicked = []; }")
    page.evaluate(
        "() => document.querySelectorAll('button').forEach("
        "(b) => b.addEventListener('click', () => window.__clicked.push(b.id)))"
    )

    report = fill_one(page, Assignment(intent="advance", locator=locator, fieldId=None), SETTINGS)

    assert report.ok is False
    assert report.blocked["control"] == locator
    assert page.evaluate("() => window.__clicked") == []


def test_an_innocuous_button_inside_a_payment_form_is_refused(page) -> None:
    """Enclosure is its own signal: "Review" says nothing, its form says everything."""
    reason = denial_reason(resolve(page, "#innocuous-btn"))

    assert reason is not None
    assert "payment context" in reason or "card details" in reason


def test_an_ordinary_button_is_not_refused(page) -> None:
    """The denylist must not block the crawl it exists to protect."""
    assert denial_reason(resolve(page, "#next-btn")) is None
    assert denial_reason(resolve(page, "#quote-btn")) is None


def test_advance_clicks_an_allowed_button(page) -> None:
    page.evaluate(
        "() => document.getElementById('next-btn')"
        ".addEventListener('click', () => { window.__advanced = true; })"
    )

    report = fill_one(page, Assignment(intent="advance", locator="#next-btn", fieldId=None), SETTINGS)

    assert report.ok
    assert page.evaluate("() => window.__advanced") is True


def test_an_unreadable_element_is_a_denial_not_a_pass(page) -> None:
    """"Could not tell" must never resolve to "safe" for an irreversible action."""
    detached = page.locator("#gone-from-the-dom")

    assert denial_reason(detached) is not None


# --------------------------------------------------------------------------- #
# Credentials never leave the machine they are typed on
# --------------------------------------------------------------------------- #


def test_a_password_is_typed_but_the_report_carries_the_placeholder(page) -> None:
    """This report becomes the metadata artifact's login stage, which is persisted."""
    report = fill_one(
        page,
        Assignment(intent="fill", locator="#legalName", fieldId="q_001", value="$PASSWORD"),
        SETTINGS,
    )

    assert report.ok
    assert report.valueUsed == "$PASSWORD"
    assert page.locator("#legalName").input_value() == "s3cret-not-in-any-report"
    assert "s3cret" not in report.model_dump_json()


def test_an_email_placeholder_resolves_to_the_configured_username(page) -> None:
    report = fill_one(
        page,
        Assignment(intent="fill", locator="#legalName", fieldId="q_001", value="$EMAIL"),
        SETTINGS,
    )

    assert report.valueUsed == "$EMAIL"
    assert page.locator("#legalName").input_value() == "agent@example.com"


def test_a_credential_never_reaches_the_log(page, caplog) -> None:
    with caplog.at_level("DEBUG", logger="trailblazer"):
        fill_one(
            page,
            Assignment(intent="fill", locator="#legalName", fieldId="q_001", value="$PASSWORD"),
            SETTINGS,
        )

    assert "s3cret" not in caplog.text


def test_an_otp_is_blocked_because_nothing_stores_one(page) -> None:
    report = fill_one(
        page,
        Assignment(intent="fill", locator="#legalName", fieldId="q_001", value="$OTP"),
        SETTINGS,
    )

    assert report.ok is False
    assert "$OTP cannot be resolved" in report.blocked["whatYouTried"]
    assert page.locator("#legalName").input_value() == ""


# --------------------------------------------------------------------------- #
# A locator that does not resolve
# --------------------------------------------------------------------------- #


def test_an_unresolvable_locator_gives_a_blocked_report_not_an_exception(page) -> None:
    """The filler never repairs a locator, and never takes the crawl down over one."""
    report = fill_one(
        page,
        Assignment(intent="fill", locator="#nothing-here", fieldId="q_099", value="x"),
        SETTINGS,
    )

    assert report.ok is False
    assert report.fieldId == "q_099"
    assert report.locator == "#nothing-here"
    assert "matched no element" in report.blocked["whatYouTried"]


def test_a_non_unique_locator_is_blocked_rather_than_acting_on_the_first_match(page) -> None:
    """Acting on the first of two would record a value against a field that never got it."""
    with pytest.raises(LocatorError, match="not unique"):
        resolve(page, "input[type=radio]")

    report = fill_one(
        page, Assignment(intent="check", locator="input[type=radio]", fieldId="q_005"), SETTINGS
    )

    assert report.ok is False
    assert not page.locator("#priorClaimsYes").is_checked()


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


def test_every_step_is_recorded_on_the_ledger(page, monkeypatch) -> None:
    """An agent that does not record its steps is invisible in the run's accounting."""
    _no_llm(monkeypatch, "123456789")
    ledger = RunLedger(job_id="test-job")

    fill_one(page, Assignment(intent="fill", locator="#fein", fieldId="q_002"), SETTINGS, ledger)
    fill_one(page, Assignment(intent="expand", locator="#agency", fieldId="q_007"), SETTINGS, ledger)
    fill_one(page, Assignment(intent="advance", locator="#pay-btn", fieldId=None), SETTINGS, ledger)

    assert [s.action for s in ledger.steps] == ["fill", "expand", "advance"]
    assert [s.ok for s in ledger.steps] == [True, True, False]
    assert all(s.agent == "form_filler" for s in ledger.steps)
    # The LLM cost of the fill is on the fill step and nowhere else.
    assert ledger.steps[0].usd == pytest.approx(0.0012)
    assert ledger.steps[1].usd == 0.0
    assert ledger.by_agent()["form_filler"]["failed"] == 1


def test_a_step_with_no_llm_call_records_no_cost(page) -> None:
    ledger = RunLedger(job_id="test-job")

    fill_one(
        page,
        Assignment(intent="fill", locator="#legalName", fieldId="q_001", value="Acme"),
        SETTINGS,
        ledger,
    )

    assert ledger.total_usd() == 0.0
    assert ledger.steps[0].unpriced is False


# --------------------------------------------------------------------------- #
# The constraint mask
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value,mask",
    [
        ("123456789", "999999999"),
        ("12-3456789", "99-9999999"),
        ("2026-01-31", "9999-99-99"),
        ("CA", "AA"),
        ("(555) 123-4567", "(999) 999-9999"),
    ],
)
def test_shape_of_records_the_format_the_page_accepted(value: str, mask: str) -> None:
    """The error text often states no format, so the accepted value is the evidence."""
    assert _shape_of(value) == mask
