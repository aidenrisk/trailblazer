"""
FormFiller, driven the way Loop will drive it: one Assignment in, one FillReport
out, against a real page.

No Loop, no Frontier, no Scraper — this agent is built and proven alone. Every
test hands `execute()` a single Assignment and then asks the DOM whether what
the report claims actually happened.
"""

import pytest

from trailblazer.agents.form_filler.form_filler import FormFiller
from trailblazer.contracts import Assignment, Option, PageDescription

from .conftest import JOB, fixed

STAGE = "form_page_1_business_info"


def page_description(next_locator="#realNext", back=None):
    """The bit of the PageDescription navigation needs: where Next lives."""
    return PageDescription(
        stageId=STAGE,
        url="file:///form_page.html",
        controls=[],
        next=next_locator,
        back=back,
    )


def fill(field_id, locator):
    return Assignment(type="fill_field", fieldId=field_id, locator=locator)


def choose(field_id, label, option_locator, control_locator):
    return Assignment(
        type="set_option",
        fieldId=field_id,
        option=Option(label=label, locator=option_locator),
        locator=control_locator,
    )


class TestPlainFields:
    def test_fill_lands_in_the_dom(self, filler, page):
        report = filler.execute(JOB, STAGE, fill("q_004", "#businessName"))

        assert report.ok is True
        assert report.landed == ["q_004"]
        # The report says what it typed...
        assert report.steps[0].value == "Test Value"
        assert report.steps[0].action == "fill"
        # ...and the page agrees. This is the assertion a fake Page can't make.
        assert page.input_value("#businessName") == "Test Value"

    def test_a_plain_field_reports_no_options(self, filler):
        report = filler.execute(JOB, STAGE, fill("q_004", "#businessName"))

        # None, not [] — "this is not a chooser, leave what you know alone".
        assert report.discoveredOptions is None
        assert report.chosenOption is None

    def test_required_flag_comes_off_the_element(self, filler, page):
        page.eval_on_selector("#businessName", "el => el.required = true")
        report = filler.execute(JOB, STAGE, fill("q_004", "#businessName"))

        assert report.steps[0].required is True

    def test_checkbox_is_toggled(self, filler, page):
        report = filler.execute(JOB, STAGE, fill("q_010", "#priorCoverage"))

        assert report.ok is True
        assert report.steps[0].action == "toggle"
        assert report.discoveredOptions is None
        assert page.is_checked("#priorCoverage") is True

    def test_value_that_does_not_stick_is_a_validation_failure(self, page):
        """
        #strictZip drops anything that isn't five digits. A report claiming this
        landed would send Frontier on with a blank field behind it.
        """
        filler = FormFiller(page, value_picker=fixed("not-a-zip"))
        report = filler.execute(JOB, STAGE, fill("q_011", "#strictZip"))

        assert report.ok is False
        assert report.errorClass == "validation"
        assert page.input_value("#strictZip") == ""


class TestMaskedFields:
    """
    A field that reformats as you type accepted the value; it just displays it
    its own way. Read as a refusal, the live Pie page loses its FEIN and its
    premium on page one alone.
    """

    def test_a_field_that_drops_the_dashes_still_landed(self, page):
        filler = FormFiller(page, value_picker=fixed("12-3456789"))
        report = filler.execute(JOB, STAGE, fill("q_007", "#fein"))

        assert report.ok is True, report.errorClass
        assert report.landed == ["q_007"]
        assert page.input_value("#fein") == "123456789"
        # The step records what was TYPED, because that is the string a replay
        # has to reproduce — typing the display form back may not round-trip.
        assert report.steps[0].value == "12-3456789"

    def test_a_field_that_adds_separators_still_landed(self, page):
        filler = FormFiller(page, value_picker=fixed("1200"))
        report = filler.execute(JOB, STAGE, fill("q_015", "#maskedPremium"))

        assert report.ok is True, report.errorClass
        assert page.input_value("#maskedPremium") == "1,200"
        assert report.steps[0].value == "1200"

    def test_a_field_that_truncates_is_still_a_failure(self, page):
        """
        The check forgives formatting, not lost content. Six digits into a
        five-digit ZIP is a dropped character, not a mask.
        """
        filler = FormFiller(page, value_picker=fixed("123456"))
        report = filler.execute(JOB, STAGE, fill("q_007", "#fein"))
        assert report.ok is True  # 9-digit cap not reached

        filler = FormFiller(page, value_picker=fixed("1234567890123"))
        report = filler.execute(JOB, STAGE, fill("q_007", "#fein"))

        assert report.ok is False
        assert report.errorClass == "validation"
        assert page.input_value("#fein") == "123456789"

    def test_a_field_that_clears_itself_is_still_a_failure(self, page):
        filler = FormFiller(page, value_picker=fixed("$-, ()"))
        report = filler.execute(JOB, STAGE, fill("q_015", "#maskedPremium"))

        # Nothing significant was typed, so nothing can be claimed to have
        # landed — an all-formatting value must not pass by having an empty
        # normal form on both sides.
        assert report.ok is False
        assert report.errorClass == "validation"


class TestTheValuePicker:
    def test_picker_is_asked_about_the_real_control(self, page):
        """
        The picker is handed what it needs to judge: the label off the page, the
        input type, and the constraints only a live element carries.
        """
        seen = {}

        def spy(control, context="", constraints=None):
            seen.update(
                label=control.label,
                type=control.type,
                context=context,
                constraints=constraints,
            )
            return "01/01/2026"

        FormFiller(page, value_picker=spy).execute(
            JOB, STAGE, fill("q_002", "#effectiveDate")
        )

        assert seen["label"] == "Policy Effective Date"
        assert seen["type"] == "date"  # from the element, not guessed from the label
        assert seen["context"] == "Business Information"  # the page's own heading
        assert "pattern" in seen["constraints"]

    def test_number_input_is_typed_as_a_number(self, page):
        seen = {}

        def spy(control, context="", constraints=None):
            seen["type"] = control.type
            return "10000"

        FormFiller(page, value_picker=spy).execute(
            JOB, STAGE, fill("q_008", "#targetPremium")
        )

        assert seen["type"] == "number"
        assert page.input_value("#targetPremium") == "10000"

    def test_label_survives_a_widget_showing_its_own_value(self, page):
        """
        A custom combobox renders its current value as its text. Reading that
        back as the label would ask the picker about a field called "Select...".
        """
        seen = {}

        def spy(control, context="", constraints=None):
            seen["label"] = control.label
            return "x"

        filler = FormFiller(page, value_picker=spy)
        filler.execute(JOB, STAGE, fill("q_001", "#agencyProgram"))

        # It's a chooser, so the picker is never consulted at all...
        assert seen == {}
        # ...and the label it would have been given is the question, not the value.
        from trailblazer.agents.form_filler import dom

        info = dom.describe(page.locator("#agencyProgram"))
        assert info.label == "Agency / Program"


class TestNativeFormats:
    """
    A native <input type=date> parses ISO and nothing else, whatever the form
    displays. The picker is told so in its constraints, but it is a model, so
    the filler normalises anyway rather than lose the control to one stray
    format.
    """

    @pytest.mark.parametrize(
        "given,stored",
        [
            ("2026-03-15", "2026-03-15"),  # already ISO
            ("03/15/2026", "2026-03-15"),  # US, what the prompt asks for
            ("March 15, 2026", "2026-03-15"),
        ],
    )
    def test_dates_are_coerced_to_what_the_element_parses(self, page, given, stored):
        filler = FormFiller(page, value_picker=fixed(given))
        report = filler.execute(JOB, STAGE, fill("q_002", "#effectiveDate"))

        assert report.ok is True, report.errorClass
        assert page.input_value("#effectiveDate") == stored
        # The step records what actually landed, so a replay uses this and not
        # the human-shaped string.
        assert report.steps[0].value == stored

    def test_an_unreadable_date_is_rejected_not_crashed(self, page):
        filler = FormFiller(page, value_picker=fixed("sometime next spring"))
        report = filler.execute(JOB, STAGE, fill("q_002", "#effectiveDate"))

        assert report.ok is False
        assert report.errorClass == "validation"
        assert page.input_value("#effectiveDate") == ""

    def test_the_picker_is_told_the_format_the_element_wants(self, page):
        seen = {}

        def spy(control, context="", constraints=None):
            seen.update(constraints or {})
            return "2026-03-15"

        FormFiller(page, value_picker=spy).execute(
            JOB, STAGE, fill("q_002", "#effectiveDate")
        )

        assert seen["format"] == "YYYY-MM-DD"

    def test_a_text_field_gets_no_format_constraint(self, page):
        seen = {}

        def spy(control, context="", constraints=None):
            seen.update(constraints or {})
            return "Harbor Point"

        FormFiller(page, value_picker=spy).execute(
            JOB, STAGE, fill("q_004", "#businessName")
        )

        assert seen["format"] is None


class TestDiscovery:
    def test_native_select_is_discovered_with_per_option_locators(self, filler, page):
        """A <select> asked to be 'filled' turns out to be a chooser."""
        report = filler.execute(JOB, STAGE, fill("q_006", "#entityType"))

        assert report.ok is True
        assert [o.label for o in report.discoveredOptions] == [
            "Limited Liability Company",
            "Corporation",
            "Sole Proprietor",
        ]
        # Each option carries its own locator, exact-match so "Corporation"
        # cannot also select an "S Corporation".
        assert report.discoveredOptions[1].locator == (
            '#entityType >> option:text-is("Corporation")'
        )
        # It took the first one, so the page is left in a real state.
        assert report.chosenOption == "Limited Liability Company"
        assert page.input_value("#entityType") == "llc"

    def test_placeholder_is_not_an_option(self, filler):
        report = filler.execute(JOB, STAGE, fill("q_006", "#entityType"))

        labels = [o.label for o in report.discoveredOptions]
        assert "Select..." not in labels

    def test_custom_widget_is_opened_to_find_its_options(self, filler, page):
        """
        #agencyProgram renders no options until it is clicked — the case Scraper
        reports as `options: null` and only the filler can resolve.
        """
        assert page.locator("[role=option]").count() == 0

        report = filler.execute(JOB, STAGE, fill("q_001", "#agencyProgram"))

        assert report.ok is True
        assert [o.label for o in report.discoveredOptions] == [
            "Pie Direct",
            "Pie Partner Program",
        ]
        assert report.chosenOption == "Pie Direct"
        assert page.inner_text("#agencyProgram") == "Pie Direct"

    def test_discovered_option_locators_resolve_to_one_element(self, filler, page):
        report = filler.execute(JOB, STAGE, fill("q_001", "#agencyProgram"))

        # Reopen so the menu exists again, then check every locator handed out
        # actually addresses exactly one thing.
        page.click("#agencyProgram")
        for option in report.discoveredOptions:
            assert page.locator(option.locator).count() == 1, option.locator

    def test_readonly_input_is_a_chooser_not_a_text_field(self, filler, page):
        """
        The live Pie shape: <input type="text" role="listbox" readonly>.

        Nothing about the tag says chooser, and fill() on it does not fail fast
        — it waits 30s for the element to become editable, then reports the
        field as having refused a value. Read as TEXT this control costs a
        timeout AND loses every option it had.
        """
        assert page.locator("[role=option]").count() == 0

        report = filler.execute(JOB, STAGE, fill("q_014", "#stateOfOperation"))

        assert report.ok is True
        assert report.steps[0].action == "select"
        assert [o.label for o in report.discoveredOptions] == [
            "California",
            "New York",
            "Texas",
        ]
        assert report.chosenOption == "California"
        assert page.input_value("#stateOfOperation") == "California"

    def test_a_readonly_field_is_never_typed_into(self, page):
        """The value picker is not even consulted: there is nothing to type."""
        asked = []

        def spy(control, context="", constraints=None):
            asked.append(control.label)
            return "x"

        FormFiller(page, value_picker=spy).execute(
            JOB, STAGE, fill("q_014", "#stateOfOperation")
        )

        assert asked == []

    def test_disabled_control_is_reported_without_being_touched(self, filler, page):
        """
        The live #agencyProgram is disabled — fixed by the account. Playwright
        does not fail fast on it: fill() and click() both wait for the element
        to become editable/enabled and only give up at the action timeout.

        Reported as an empty chooser: ok, so the walk does not end on a
        legitimate page state, and explored, so it does not block the controls
        after it. No step, because nothing was done.
        """
        report = filler.execute(JOB, STAGE, fill("q_016", "#lockedProgram"))

        assert report.ok is True
        assert report.discoveredOptions == []
        assert report.landed == ["q_016"]
        assert report.steps == []
        # Untouched: the value it shipped with is still there.
        assert page.input_value("#lockedProgram") == "Pie Direct"

    def test_a_disabled_control_fails_fast(self, filler):
        """
        The whole point. Acting on it first would cost 30s of action timeout,
        and there are three such controls on the live page's first form.
        """
        import time

        start = time.monotonic()
        filler.execute(JOB, STAGE, fill("q_016", "#lockedProgram"))

        assert time.monotonic() - start < 5

    def test_widget_with_no_options_reports_empty_not_none(self, filler):
        """
        [] means "opened it, it genuinely has none". Reporting None instead
        would leave the control forever unexplored and block the whole page.
        """
        report = filler.execute(JOB, STAGE, fill("q_013", "#emptyMenu"))

        assert report.ok is True
        assert report.discoveredOptions == []
        assert report.chosenOption is None


class TestSetOption:
    def test_native_select_is_set_by_label_on_the_parent(self, filler, page):
        """
        A native <option> cannot be clicked — the list is drawn by the OS — so
        this is the one case where the control's own locator is what to act on.
        """
        report = filler.execute(
            JOB,
            STAGE,
            choose(
                "q_006",
                "Sole Proprietor",
                '#entityType >> option:text-is("Sole Proprietor")',
                "#entityType",
            ),
        )

        assert report.ok is True
        assert report.chosenOption == "Sole Proprietor"
        assert page.input_value("#entityType") == "sole"

    def test_split_control_clicks_the_options_own_locator(self, filler, page):
        """
        Two buttons, no shared parent. On the real Pie page the control's
        locator is byte-identical to the "Yes" option's, so acting on the
        control could only ever press Yes.
        """
        report = filler.execute(
            JOB, STAGE, choose("q_009", "No", "#locationsNo", "#locationsYes")
        )

        assert report.ok is True
        assert report.steps[0].locator == "#locationsNo"
        assert page.get_attribute("#locationsNo", "aria-pressed") == "true"
        assert page.get_attribute("#locationsYes", "aria-pressed") == "false"

    def test_widget_is_opened_when_the_option_is_not_on_the_page(self, filler, page):
        report = filler.execute(
            JOB,
            STAGE,
            choose(
                "q_001",
                "Pie Partner Program",
                'role=option[name="Pie Partner Program"]',
                "#agencyProgram",
            ),
        )

        assert report.ok is True
        assert page.inner_text("#agencyProgram") == "Pie Partner Program"

    def test_choosing_llc_reveals_the_members_field(self, filler, page):
        """
        The filler reports the choice and nothing about the reveal. Noticing new
        controls is Scraper's job and attributing them is Frontier's — neither
        is here, and the filler must not grow opinions about either.
        """
        report = filler.execute(
            JOB,
            STAGE,
            choose(
                "q_006",
                "Limited Liability Company",
                '#entityType >> option:text-is("Limited Liability Company")',
                "#entityType",
            ),
        )

        assert report.ok is True
        assert page.is_visible("#membersRow") is True
        assert [s.fieldId for s in report.steps] == ["q_006"]


class TestFailures:
    def test_missing_locator_is_not_found(self, filler):
        report = filler.execute(JOB, STAGE, fill("q_404", "#noSuchField"))

        assert report.ok is False
        assert report.errorClass == "not_found"
        assert report.fieldId == "q_404"

    def test_ambiguous_locator_is_not_unique(self, filler, page):
        """
        Two elements share .duplicateName. Filling `.first` would silently type
        into whichever came first in the DOM and nothing downstream would catch
        it, so this has to fail instead.
        """
        assert page.locator(".duplicateName").count() == 2

        report = filler.execute(JOB, STAGE, fill("q_dup", ".duplicateName"))

        assert report.ok is False
        assert report.errorClass == "not_unique"
        assert page.input_value("input[name=dupe1]") == ""

    def test_no_recovery_configured_means_the_failure_stands(self, filler):
        assert filler.recovery is None
        report = filler.execute(JOB, STAGE, fill("q_404", "#noSuchField"))

        assert report.ok is False

    def test_recovery_is_offered_the_failure(self, page):
        """Recovery fires on a locator miss, and only on a locator miss."""
        offered = []

        class SpyRecovery:
            def attempt(self, job, assignment, failed, filler):
                offered.append(failed.errorClass)
                return failed

        filler = FormFiller(page, value_picker=fixed(), recovery=SpyRecovery())
        filler.execute(JOB, STAGE, fill("q_404", "#noSuchField"))

        assert offered == ["not_found"]

    def test_recovery_is_not_offered_a_rejected_value(self, page):
        """
        A field refusing a value is the page talking. Looking at it harder will
        not change its mind, so no model call is spent on it.
        """
        offered = []

        class SpyRecovery:
            def attempt(self, job, assignment, failed, filler):
                offered.append(failed.errorClass)
                return failed

        filler = FormFiller(
            page, value_picker=fixed("not-a-zip"), recovery=SpyRecovery()
        )
        report = filler.execute(JOB, STAGE, fill("q_011", "#strictZip"))

        assert report.errorClass == "validation"
        assert offered == []


class TestNavigation:
    def test_next_that_navigates_reports_advance(self, filler, page):
        report = filler.execute(
            JOB, STAGE, Assignment(type="next"), page_description("#realNext")
        )

        assert report.ok is True
        assert report.advance is True
        assert page.url.endswith("form_page_2.html")

    def test_next_that_does_nothing_reports_no_advance(self, filler, page):
        """
        `advance` is answered on evidence, never on intent. A Loop keys its page
        counter off this flag, so a click that claimed success while the page
        stood still would rename the stage and lose the board.
        """
        report = filler.execute(
            JOB, STAGE, Assignment(type="next"), page_description("#deadNext")
        )

        assert report.ok is True
        assert report.advance is False
        assert page.url.endswith("form_page.html")

    def test_next_without_a_page_description_fails_loudly(self, filler):
        """
        The Assignment carries no locator, so with no page there is nothing to
        click. Better to say so than to guess at a button.
        """
        report = filler.execute(JOB, STAGE, Assignment(type="next"))

        assert report.ok is False
        assert report.errorClass == "not_found"

    def test_stop_does_nothing_and_says_so(self, filler, page):
        before = page.url
        report = filler.execute(JOB, STAGE, Assignment(type="stop"))

        assert report.ok is True
        assert report.steps == []
        assert page.url == before


class TestOneAssignmentAtATime:
    def test_filling_one_control_leaves_the_others_alone(self, filler, page):
        filler.execute(JOB, STAGE, fill("q_004", "#businessName"))

        assert page.input_value("#businessName") == "Test Value"
        for untouched in ("#fein", "#businessZipCode", "#effectiveDate"):
            assert page.input_value(untouched) == "", untouched
        assert page.is_checked("#priorCoverage") is False
