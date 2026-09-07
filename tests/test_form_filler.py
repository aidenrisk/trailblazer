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
from trailblazer.agents.browser import write_tools
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

    def fake(label, locator, url, constraint_hint, error_text, settings, **scope):
        calls.append({
            "label": label,
            "error_text": error_text,
            "hint": constraint_hint,
            **scope,
        })
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
    assert [o["label"] for o in report.optionsRevealed] == [
        "Direct", "Broker Network", "Affinity Partner", "Wholesale",
    ]
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
    assert "Limited Liability Company" in [o["label"] for o in report.optionsRevealed]
    assert page.locator("#entityType").input_value() == ""


def test_expand_allows_a_formatter_rewriting_an_answered_field(page) -> None:
    """A blur that reformats a value already given changes nothing downstream.

    The artifact keeps what was typed and the same formatter runs on the same
    input at replay, so the options read while the widget was open are good.
    """
    page.fill("#phone", "5551234567")

    report = fill_one(page, Assignment(intent="expand", locator="#formatter", fieldId="q_020"), SETTINGS)

    assert report.ok, report.blocked
    assert [o["label"] for o in report.optionsRevealed] == ["Alpha", "Beta"]
    assert page.locator("#phone").input_value() == "(555) 123-4567"


def test_expand_blocks_when_the_open_answers_an_untouched_field(page) -> None:
    """A value the walk never chose will be submitted and no artifact records it."""
    report = fill_one(page, Assignment(intent="expand", locator="#committer", fieldId="q_021"), SETTINGS)

    assert not report.ok
    assert "hidden-answer" in report.blocked["whatYouTried"]
    assert report.optionsRevealed is None


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
        # Prefixed so the script can tell what the page complained about from
        # what it stated up front; both may be present in one hint.
        "hint": "rejected with: Please enter the FEIN",
    }
    assert page.locator("#fein").get_attribute("aria-invalid") == "false"


def test_the_correction_call_is_given_the_pages_error_text(page, monkeypatch) -> None:
    calls = _no_llm(monkeypatch, lambda n: "12-3456" if n == 1 else "123456789")

    fill_one(page, Assignment(intent="fill", locator="#fein", fieldId="q_002"), SETTINGS)

    assert calls[0]["error_text"] is None
    assert calls[1]["error_text"] == "Please enter the FEIN"


def test_an_accepted_digit_fill_records_its_mask_with_no_retry(page, monkeypatch) -> None:
    """No rejection, nothing stated up front -- the accepted shape is still a rule.

    The earlier rule recorded nothing without a rejection, so a FEIN answered
    correctly first time left the script unable to shape a different answer.
    """
    _no_llm(monkeypatch, "123456789")

    report = fill_one(page, Assignment(intent="fill", locator="#fein", fieldId="q_002"), SETTINGS)

    assert report.ok
    assert report.retried is False
    assert report.constraint == {"unit": "", "format": "999999999", "hint": ""}


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


def test_an_element_in_no_form_has_no_card_relationship(page) -> None:
    """A custom dropdown mounts its options at body level, outside every form.

    The page also holds a payment form. Widening the scan to the whole page
    when nothing encloses the element refused every such option on any portal
    with a payment widget anywhere; a card relationship is a shared form.
    """
    page.click("#entityPicker2")
    page.wait_for_timeout(200)

    assert denial_reason(page.locator('role=option[name="Corporation"] >> visible=true')) is None


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


# --------------------------------------------------------------------------- #
# Duplicated controls
# --------------------------------------------------------------------------- #


def test_a_link_rendered_twice_resolves_to_the_visible_copy(page) -> None:
    """A responsive nav renders one destination twice and hides the other copy."""
    assert page.locator("a.nav-start").count() == 2

    element = write_tools.resolve_click_target(page, "a.nav-start")

    assert element.is_visible()


def test_two_visible_copies_are_still_refused(page) -> None:
    """Two visible matches is a real ambiguity, not a responsive duplicate."""
    with pytest.raises(LocatorError, match="2 visible"):
        write_tools.resolve_click_target(page, "a.nav-both")


def test_matches_that_differ_are_still_refused(page) -> None:
    """Same selector, different destinations: picking one is a guess."""
    with pytest.raises(LocatorError, match="not the same control"):
        write_tools.resolve_click_target(page, "a.nav-differs")


def test_a_fill_never_takes_the_visible_one(page) -> None:
    """Two inputs sharing a selector are different fields; a value in the wrong
    one is invisible downstream, since the locator still resolves to one node."""
    with pytest.raises(LocatorError, match="not unique"):
        write_tools.fill(page, "input", "anything")


def test_the_pages_own_format_reaches_the_chooser_before_any_rejection(
    page, monkeypatch
) -> None:
    """A rejection costs a page round trip and a second model call to learn
    what the placeholder already said."""
    calls = _no_llm(monkeypatch, "01/15/2027")

    fill_one(
        page,
        Assignment(
            intent="fill",
            locator="#effectiveDate",
            fieldId="q_030",
            constraintHint="MM/DD/YYYY; at most 10 characters; Two-digit month and day",
        ),
        SETTINGS,
    )

    assert calls[0]["hint"] == "MM/DD/YYYY; at most 10 characters; Two-digit month and day"
    assert calls[0]["error_text"] is None
    assert page.locator("#effectiveDate").input_value() == "01/15/2027"


def test_the_control_type_reaches_the_chooser(page, monkeypatch) -> None:
    """`input[type=date]` states its shape with no placeholder at all."""
    calls = _no_llm(monkeypatch, "Acme")

    fill_one(page, Assignment(intent="fill", locator="#legalName", fieldId="q_001"), SETTINGS)

    assert calls[0]["control_type"] == "input[type=text]"


# --------------------------------------------------------------------------- #
# Rejection signals and the recorded constraint
# --------------------------------------------------------------------------- #


def test_a_rejection_through_the_error_slot_alone_is_detected_and_fixed(page, monkeypatch) -> None:
    """The field never sets aria-invalid; only its named error slot fills.

    One attribute was the earlier rule and this case was invisible to it.
    """
    calls = _no_llm(monkeypatch, lambda n: "123" if n == 1 else "94105")

    report = fill_one(page, Assignment(intent="fill", locator="#zipAlt", fieldId="q_040"), SETTINGS)

    assert report.ok and report.retried
    assert calls[1]["error_text"] == "Enter a 5-digit ZIP"
    assert report.constraint["format"] == "99999"
    assert "rejected with: Enter a 5-digit ZIP" in report.constraint["hint"]


def test_a_correct_first_fill_still_records_what_was_known(page, monkeypatch) -> None:
    """No rejection happened, and the script still needs the rule."""
    _no_llm(monkeypatch, "842673915")

    report = fill_one(
        page,
        Assignment(
            intent="fill", locator="#fein", fieldId="q_002",
            constraintHint="at most 9 characters", helpText="Nine digits, no dashes",
        ),
        SETTINGS,
    )

    assert report.ok and not report.retried
    assert report.constraint == {
        "unit": "",
        "format": "999999999",
        "hint": "at most 9 characters; Nine digits, no dashes",
    }


def test_free_text_records_no_mask(page, monkeypatch) -> None:
    """The shape of a business name is noise, not a rule."""
    _no_llm(monkeypatch, "Acme LLC")

    report = fill_one(page, Assignment(intent="fill", locator="#legalName", fieldId="q_001"), SETTINGS)

    assert report.ok
    assert report.constraint is None


def test_the_help_text_reaches_the_value_chooser(page, monkeypatch) -> None:
    """The tooltip is often the only place the rule is stated."""
    calls = _no_llm(monkeypatch, "842673915")

    fill_one(
        page,
        Assignment(intent="fill", locator="#fein", fieldId="q_002", helpText="Nine digits, no dashes"),
        SETTINGS,
    )

    assert calls[0]["help_text"] == "Nine digits, no dashes"


# --------------------------------------------------------------------------- #
# The value chooser survives a provider hiccup
# --------------------------------------------------------------------------- #


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content


def test_an_empty_model_reply_is_asked_again_before_it_fails_the_run(monkeypatch) -> None:
    """One empty reply on the ZIP field ended a live run. Asked again, it answered."""
    from trailblazer.agents.form_filler import values
    from trailblazer.shared import models

    replies = iter(["", "", "94105"])

    class _Model:
        def invoke(self, messages, config=None):
            return _Reply(next(replies))

    monkeypatch.setattr(values, "get_model", lambda settings: _Model())
    monkeypatch.setattr(models.time, "sleep", lambda s: None)

    value, _, _ = values.choose_value("Business Zip Code", "#zip", "https://x", None, None, SETTINGS)

    assert value == "94105"


def test_a_reply_that_stays_empty_fails_naming_the_field(monkeypatch) -> None:
    from trailblazer.agents.form_filler import values
    from trailblazer.shared import models

    class _Model:
        def invoke(self, messages, config=None):
            return _Reply("")

    monkeypatch.setattr(values, "get_model", lambda settings: _Model())
    monkeypatch.setattr(models.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError, match="no value for field 'Business Zip Code' after 3 attempts"):
        values.choose_value("Business Zip Code", "#zip", "https://x", None, None, SETTINGS)


# --------------------------------------------------------------------------- #
# A chooser whose options mount into a popper with no listbox role
# --------------------------------------------------------------------------- #


def test_expand_reads_only_the_options_that_appeared_never_the_nav(page) -> None:
    """Pie's Legal Entity Type. The earlier reader returned "Dashboard"."""
    report = fill_one(page, Assignment(intent="expand", locator="#entityPicker2", fieldId="q_050"), SETTINGS)

    assert report.ok
    labels = [o["label"] for o in report.optionsRevealed]
    assert labels == ["Corporation", "Partnership", "Limited Liability Company"]
    assert "Dashboard" not in labels and "Appetite Checker" not in labels
    # Each option is addressable by role and its own text, measured unique.
    assert report.optionsRevealed[0]["locator"] == 'role=option[name="Corporation"] >> visible=true'
    # The widget is closed again and the page holds no popper.
    assert page.locator(".popper").count() == 0
    assert page.locator("#entityPicker2").input_value() == ""


def test_select_opens_a_closed_chooser_and_clicks_the_option(page) -> None:
    """An option in a closed chooser is not in the DOM; the select mounts it first."""
    report = fill_one(
        page,
        Assignment(
            intent="select", locator="#entityPicker2", fieldId="q_050",
            value="Partnership", optionLocator='role=option[name="Partnership"] >> visible=true',
        ),
        SETTINGS,
    )

    assert report.ok and report.valueUsed == "Partnership"
    assert page.locator("#entityPicker2").input_value() == "Partnership"
    assert page.locator(".popper").count() == 0


def test_a_chooser_that_mounts_nothing_reveals_no_options(page) -> None:
    """A read-only field with no list behind it is not given the nav as its options."""
    report = fill_one(page, Assignment(intent="expand", locator="#agencyProgram", fieldId="q_051"), SETTINGS)

    # Not a disclosure at all, or a disclosure that mounted nothing: either way
    # the answer is no options, never someone else's list.
    labels = [o["label"] for o in (report.optionsRevealed or [])]
    assert "Dashboard" not in labels and "Appetite Checker" not in labels


# --------------------------------------------------------------------------- #
# A non-gate dropdown: the filler chooses among the options
# --------------------------------------------------------------------------- #


def test_a_select_with_no_value_asks_the_model_to_pick_one_option_and_clicks_it(
    page, monkeypatch
) -> None:
    """Five entity types is not a gate; someone still has to pick one."""
    calls = _no_llm(monkeypatch, "Partnership")
    options = [
        {"label": k, "locator": f'role=option[name="{k}"] >> visible=true'}
        for k in ("Corporation", "Partnership", "Limited Liability Company")
    ]

    report = fill_one(
        page,
        Assignment(intent="select", locator="#entityPicker2", fieldId="q_050", options=options),
        SETTINGS,
    )

    assert report.ok and report.valueUsed == "Partnership"
    assert calls[0]["options"] == ["Corporation", "Partnership", "Limited Liability Company"]
    assert page.locator("#entityPicker2").input_value() == "Partnership"


def test_a_native_select_with_no_value_is_set_by_the_chosen_label(page, monkeypatch) -> None:
    _no_llm(monkeypatch, "Limited Liability Company")
    options = [{"label": k, "locator": None} for k in ("Select...", "Sole Proprietor", "Limited Liability Company", "Corporation")]

    report = fill_one(
        page,
        Assignment(intent="select", locator="#entityType", fieldId="q_004", options=options),
        SETTINGS,
    )

    assert report.ok and report.valueUsed == "Limited Liability Company"
    assert page.locator("#entityType").input_value() == "llc"


def test_a_choice_outside_the_listed_options_is_refused_not_typed(page, monkeypatch) -> None:
    """The model's answer must be one of the page's choices, character for character."""
    _no_llm(monkeypatch, "LLC")
    options = [{"label": k, "locator": None} for k in ("Sole Proprietor", "Limited Liability Company")]

    report = fill_one(
        page,
        Assignment(intent="select", locator="#entityType", fieldId="q_004", options=options),
        SETTINGS,
    )

    assert not report.ok
    assert "not one of" in report.blocked["whatYouTried"]


def test_a_pinned_select_with_a_value_but_no_locator_still_clicks_its_option(page) -> None:
    """Re-execution keeps the chosen value, not the locator the first pass found.

    On a live run this fell through to the native-select method on a custom
    listbox, blocked in 10ms, aborted the re-execution and cost a real gate its
    second side. The options are still on the assignment; the locator is looked
    up by label.
    """
    options = [
        {"label": k, "locator": f'role=option[name="{k}"] >> visible=true'}
        for k in ("Corporation", "Partnership", "Limited Liability Company")
    ]

    report = fill_one(
        page,
        Assignment(intent="select", locator="#entityPicker2", fieldId="q_050",
                   value="Limited Liability Company", options=options),
        SETTINGS,
    )

    assert report.ok and report.valueUsed == "Limited Liability Company"
    assert page.locator("#entityPicker2").input_value() == "Limited Liability Company"


def test_page_problems_reads_empty_invalid_and_unchosen_off_the_dom(page) -> None:
    """The check before Next and after a Next that changed nothing. No model."""
    from trailblazer.agents.form_filler.form_filler import page_problems

    page.fill("#zipAlt", "123")           # rejects through its error slot
    page.fill("#legalName", "Acme LLC")   # fine

    found = {p["locator"]: p["problem"] for p in page_problems(page)}

    assert found["#zipAlt"] == "Enter a 5-digit ZIP"
    assert "#legalName" not in found
    assert found["#fein"] == "empty"                               # required, untouched
    assert found['[name="priorClaims"]'] == "no option chosen"      # radio group, none checked
    assert "#agencyProgram" not in found                           # locked, not settable
    assert found["#entityPicker2"] == "empty"                      # read-only chooser is settable


def test_fill_leaves_the_field_so_a_blur_committed_value_is_committed(page) -> None:
    """A framework input takes the value when focus leaves; fill alone never moved it."""
    write_tools.fill(page, "#committed", "https://acme.example")

    assert page.locator("#committed").get_attribute("data-committed") == "https://acme.example"
