"""
FormFiller: execute exactly ONE Assignment against a live page.

    Assignment in  ->  do the one thing it says  ->  FillReport out

It is the only agent that touches the form, and the only one holding a real
element, which makes it the answer to two questions nobody else can answer:

  what to type    Frontier's Assignment carries no value. FormFiller sees the
                  label, the input type and the page it sits on, so FormFiller
                  decides — see value_picker.py.

  what this is    Scraper reports `options: null` for any widget that doesn't
                  render its list until opened. FormFiller opens it, and reports
                  what it found on FillReport.discoveredOptions. That is the
                  whole discovery channel; Frontier then walks every option.

Mechanics are deterministic on purpose. The Assignment already carries an exact
locator, and for a set_option the option's own locator too, so "click this" needs
no judgment and a model would only paper over a locator bug that should surface.
A model is consulted in exactly one place: when a locator misses, `recovery`
gets one attempt to land the action, and the result is re-verified against the
DOM before it is believed.
"""

import datetime
import logging
import re

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.agents.form_filler import dom
from trailblazer.agents.form_filler.value_picker import ValuePicker, rule_based
from trailblazer.contracts import (
    Assignment,
    FillReport,
    FillStep,
    Option,
    PageDescription,
)

logger = logging.getLogger(__name__)

# How long to wait for a navigation after a Next/Back/Submit click before
# concluding the page did not move.
NAVIGATION_TIMEOUT_MS = 3_000

# What a native <input type=...> demands, regardless of what the form displays.
# A date picker rendering "03/15/2026" still only accepts an ISO value, and
# fill() raises "Malformed value" on anything else.
NATIVE_FORMATS = {
    "date": "YYYY-MM-DD",
    "month": "YYYY-MM",
    "time": "HH:MM",
    "datetime-local": "YYYY-MM-DDTHH:MM",
}

# Characters a form adds or removes purely to display a value: the separators in
# a currency field, the dashes in a FEIN, the brackets and spaces in a phone
# number. Ignored when checking whether a fill landed.
_FORMATTING = re.compile(r"[\s,()\-–—$]")

_DATE_ORDERS = (
    "%Y-%m-%d",  # already ISO
    "%m/%d/%Y",  # US, what a person writes and what the prompt asks for
    "%m-%d-%Y",
    "%d/%m/%Y",
    "%B %d, %Y",
    "%b %d, %Y",
)


class FormFiller:
    """
    Args:
    - page: the live tab. A Page, never a browser session — the caller owns the
            browser, exactly as the real Scraper's `perceive(page, ...)` does.
    - value_picker: what to type into a plain field. Defaults to the offline
            rule table; pass `LLMValuePicker()` for judgment.
    - recovery: one attempt to rescue an assignment whose locator missed.
            None (the default) disables it, which is what keeps tests offline.
    """

    def __init__(
        self,
        page: Page,
        value_picker: ValuePicker | None = None,
        recovery=None,
    ) -> None:
        self.page = page
        self.value_picker: ValuePicker = value_picker or rule_based
        self.recovery = recovery

    # ------------------------------------------------------------------
    # The one entry point
    # ------------------------------------------------------------------

    def execute(
        self,
        job: str,
        stage_id: str,
        assignment: Assignment,
        page_description: PageDescription | None = None,
    ) -> FillReport:
        """
        Do the one thing this Assignment says, and report what happened.

        `page_description` is needed only for navigation: `Assignment(type="next")`
        carries no locator — the contract forbids control fields on navigation
        types — so the Next button's address has to come from the page the
        caller is holding.
        """
        try:
            if assignment.type == "fill_field":
                return self._fill_field(job, assignment)
            if assignment.type == "set_option":
                return self._set_option(job, assignment)
            if assignment.type in ("next", "back", "submit"):
                return self._navigate(job, assignment, page_description)
            if assignment.type == "stop":
                return FillReport(ok=True)
        except PlaywrightError as e:
            # Anything Playwright raises that the paths below didn't anticipate.
            # A walk should end with a report, never a traceback.
            logger.error("[%s] %s on %s: %s", job, assignment.type, assignment.fieldId, e)
            return FillReport(ok=False, fieldId=assignment.fieldId, errorClass="widget")

        logger.warning("[%s] nothing to do for %s", job, assignment.type)
        return FillReport(ok=True)

    # ------------------------------------------------------------------
    # fill_field — which is also how a hidden chooser gets discovered
    # ------------------------------------------------------------------

    def _fill_field(self, job: str, assignment: Assignment) -> FillReport:
        target, error = dom.resolve(self.page, assignment.locator)
        if target is None:
            return self._recover(job, assignment, error)

        info = dom.describe(target)
        kind = info.kind
        logger.info(
            "[%s] %s is %s (%r)", job, assignment.fieldId, kind, info.label
        )

        if info.disabled:
            return self._unavailable(job, assignment)

        if kind == dom.ElementInfo.NATIVE_SELECT:
            return self._fill_native_select(job, assignment, target, info)
        if kind == dom.ElementInfo.WIDGET:
            return self._fill_widget(job, assignment, target, info)
        if kind == dom.ElementInfo.TOGGLE:
            return self._fill_toggle(job, assignment, target, info)
        return self._fill_text(job, assignment, target, info)

    def _fill_text(self, job, assignment, target, info) -> FillReport:
        """A plain input. Type a value, then check the page kept it."""
        value = self._value_for(assignment, info)

        # A native date/time input takes ISO and nothing else. The picker is
        # told that in its constraints, but it is a model, so normalise anyway
        # rather than let one stray format end the control.
        if info.input_type in NATIVE_FORMATS:
            normalized = _to_native(value, info.input_type)
            if normalized is None:
                logger.warning(
                    "[%s] %s wants %s and %r is not that",
                    job, assignment.fieldId, NATIVE_FORMATS[info.input_type], value,
                )
                return self._rejected(assignment, info, value)
            value = normalized

        try:
            target.fill(value)
        except PlaywrightError as e:
            # The element refused the value outright (a mask, a malformed
            # native value). That is the page talking, not a bad locator.
            logger.warning("[%s] %s refused %r: %s", job, assignment.fieldId, value, e)
            return self._rejected(assignment, info, value)

        # Read it back. A field that silently drops what was typed (a 5-digit
        # ZIP given 6, an input that clears itself) leaves the form incomplete,
        # and a report claiming it landed would send Frontier on to the next
        # control with a blank behind it.
        landed = self._read_back(target, info)
        if landed is not None and not _kept(value, landed):
            logger.warning(
                "[%s] %s did not keep %r (page shows %r)",
                job, assignment.fieldId, value, landed,
            )
            return self._rejected(assignment, info, value)

        if landed is not None and landed != value:
            # Accepted, then reformatted. Worth saying out loud, because the
            # step below records what was TYPED and not what is displayed —
            # that is the string a replay has to reproduce.
            logger.info(
                "[%s] %s shows %r for the %r it was given; the value landed",
                job, assignment.fieldId, landed, value,
            )

        logger.info("[%s] typed %r into %s", job, value, assignment.fieldId)
        return FillReport(
            ok=True,
            steps=[
                FillStep(
                    fieldId=assignment.fieldId,
                    action="fill",
                    locator=assignment.locator,
                    value=value,
                    required=info.required,
                )
            ],
            landed=[assignment.fieldId],
            fieldId=assignment.fieldId,
            discoveredOptions=None,  # not a chooser
        )

    @staticmethod
    def _rejected(assignment: Assignment, info, value: str) -> FillReport:
        """
        The page would not take this value.

        Reported as `validation` and never sent to recovery: a field refusing a
        value is the page talking, and looking at it harder will not change its
        mind. The value is kept on the step so the log says what was refused.
        """
        return FillReport(
            ok=False,
            fieldId=assignment.fieldId,
            errorClass="validation",
            steps=[
                FillStep(
                    fieldId=assignment.fieldId,
                    action="fill",
                    locator=assignment.locator,
                    value=value,
                    required=info.required,
                )
            ],
        )

    def _fill_toggle(self, job, assignment, target, info) -> FillReport:
        target.check()
        logger.info("[%s] checked %s", job, assignment.fieldId)
        return FillReport(
            ok=True,
            steps=[
                FillStep(
                    fieldId=assignment.fieldId,
                    action="toggle",
                    locator=assignment.locator,
                    value="true",
                    required=info.required,
                )
            ],
            landed=[assignment.fieldId],
            fieldId=assignment.fieldId,
            discoveredOptions=None,
        )

    def _fill_native_select(self, job, assignment, target, info) -> FillReport:
        """
        It was never a field: it's a <select>, and its choices are already here.

        Take the first one so the page is left in a real state, and report all
        of them so Frontier can come back for the rest.
        """
        options = dom.native_select_options(assignment.locator, info)
        if not options:
            return self._empty_chooser(job, assignment, "no selectable options")

        chosen = options[0]
        target.select_option(label=chosen.label)
        logger.info(
            "[%s] %s is a <select>: %s — picking %s",
            job, assignment.fieldId, [o.label for o in options], chosen.label,
        )
        return self._chooser_report(assignment, chosen, options, info)

    def _fill_widget(self, job, assignment, target, info) -> FillReport:
        """
        A custom widget. The only way to know what it offers is to open it.

        This is the case Scraper cannot report — the list is not in the DOM
        until the click — and it is why fill_field doubles as discovery.
        """
        options = dom.open_widget(self.page, target)
        if not options:
            return self._empty_chooser(job, assignment, "opened, no options rendered")

        chosen = options[0]
        option_target, error = dom.resolve(self.page, chosen.locator)
        if option_target is None:
            logger.warning(
                "[%s] discovered %s but its locator %r is %s",
                job, chosen.label, chosen.locator, error,
            )
            return FillReport(
                ok=False, fieldId=assignment.fieldId, errorClass="widget",
                discoveredOptions=options,
            )
        option_target.click()

        logger.info(
            "[%s] %s is a widget: %s — picking %s",
            job, assignment.fieldId, [o.label for o in options], chosen.label,
        )
        return self._chooser_report(assignment, chosen, options, info)

    def _unavailable(self, job: str, assignment: Assignment) -> FillReport:
        """
        The control is disabled: it cannot be typed into or clicked at all.

        Worth checking before acting rather than finding out by acting, because
        Playwright does not fail fast on this — fill() and click() both wait for
        the element to become editable/enabled and only give up at the action
        timeout, thirty seconds later. The live Pie page ships one of these
        (`#agencyProgram`, fixed by the account), so the cost is real.

        Reported the way an empty chooser is, and for the same reason: a
        disabled field is a legitimate page state, not an error, so ending the
        walk on it would be wrong — but leaving it unexplored would block every
        control after it. Nothing is claimed to have been done: no step, so
        there is nothing for a replay to reproduce.
        """
        logger.info(
            "[%s] %s (%s) is disabled; nothing to do",
            job, assignment.fieldId, assignment.locator,
        )
        return FillReport(
            ok=True,
            landed=[assignment.fieldId],
            fieldId=assignment.fieldId,
            discoveredOptions=[],
        )

    def _empty_chooser(self, job: str, assignment: Assignment, why: str) -> FillReport:
        """
        A chooser with nothing in it, reported as `[]` and not `None`.

        The distinction is load-bearing. `None` means "not a chooser, leave what
        you know alone", so a zero-option chooser reported as `None` never gets
        marked explored and blocks every control after it on the page.
        """
        logger.info("[%s] %s: %s -> reporting []", job, assignment.fieldId, why)
        return FillReport(
            ok=True,
            landed=[assignment.fieldId],
            fieldId=assignment.fieldId,
            discoveredOptions=[],
        )

    @staticmethod
    def _chooser_report(
        assignment: Assignment, chosen: Option, options: list[Option], info
    ) -> FillReport:
        return FillReport(
            ok=True,
            steps=[
                FillStep(
                    fieldId=assignment.fieldId,
                    action="select",
                    locator=chosen.locator,
                    value=chosen.label,
                    required=info.required,
                )
            ],
            landed=[assignment.fieldId],
            fieldId=assignment.fieldId,
            discoveredOptions=options,
            chosenOption=chosen.label,
        )

    # ------------------------------------------------------------------
    # set_option — act on the OPTION's locator, not the control's
    # ------------------------------------------------------------------

    def _set_option(self, job: str, assignment: Assignment) -> FillReport:
        option = assignment.option
        target, error = dom.resolve(self.page, assignment.locator)
        if target is None:
            return self._recover(job, assignment, error)

        info = dom.describe(target)

        if info.kind == dom.ElementInfo.NATIVE_SELECT:
            # A native <option> cannot be clicked — the list is drawn by the OS.
            # It is set by label on the parent, which is the one case where the
            # control's locator is the right thing to act on.
            target.select_option(label=option.label)
            acted_on = assignment.locator
        else:
            acted_on = assignment.action_locator
            option_target, error = dom.resolve(self.page, acted_on)
            if option_target is None or not option_target.is_visible():
                # Not on the page yet: the widget has to be opened first. A
                # split control (two buttons, no shared parent) skips this,
                # because its options are visible from the start — which
                # matters, since opening it means clicking one of them.
                target.click()
                option_target, error = dom.resolve(self.page, acted_on)
                if option_target is None:
                    return self._recover(job, assignment, error or "not_found")
            option_target.click()

        logger.info(
            "[%s] selected %s=%s via %s", job, assignment.fieldId, option.label, acted_on
        )
        return FillReport(
            ok=True,
            steps=[
                FillStep(
                    fieldId=assignment.fieldId,
                    action="select",
                    locator=acted_on,
                    value=option.label,
                    required=info.required,
                )
            ],
            landed=[assignment.fieldId],
            fieldId=assignment.fieldId,
            chosenOption=option.label,
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _navigate(
        self, job: str, assignment: Assignment, page_description: PageDescription | None
    ) -> FillReport:
        locator = self._navigation_locator(assignment, page_description)
        if locator is None:
            logger.error("[%s] no %s locator on the page description", job, assignment.type)
            return FillReport(ok=False, errorClass="not_found")

        target, error = dom.resolve(self.page, locator)
        if target is None:
            return FillReport(ok=False, errorClass=error)

        before = self.page.url
        target.click()
        advanced = self._did_navigate(before)

        logger.info(
            "[%s] %s clicked (%s)",
            job, assignment.type, "navigated" if advanced else "page did not move",
        )
        return FillReport(
            ok=True,
            steps=[FillStep(action="click", locator=locator)],
            advance=advanced,
        )

    @staticmethod
    def _navigation_locator(
        assignment: Assignment, page_description: PageDescription | None
    ) -> str | None:
        if page_description is None:
            return None
        if assignment.type == "back":
            return page_description.back
        return page_description.next

    def _did_navigate(self, before: str) -> bool:
        """
        Did the page actually move?

        Answered on evidence, never on intent. `advance` is what a Loop uses to
        decide it is on a new stage; a click that reported success while the
        page stood still would rename the stage under Frontier and lose every
        control it was tracking. So: a changed URL, or a load event.
        """
        try:
            self.page.wait_for_url(
                lambda url: url != before, timeout=NAVIGATION_TIMEOUT_MS
            )
            return True
        except PlaywrightError:
            return self.page.url != before

    # ------------------------------------------------------------------
    # Shared bits
    # ------------------------------------------------------------------

    def _value_for(self, assignment: Assignment, info) -> str:
        """
        What to type. The judgment call, delegated to the injected picker.

        The constraints come off the live element, because they are what makes
        the difference between a value the field accepts and one it silently
        drops.
        """
        control = info.as_control(assignment.fieldId or "field", assignment.locator)
        return self.value_picker(
            control,
            context=info.page_heading,
            constraints={
                "placeholder": info.placeholder,
                "pattern": info.pattern,
                "max_length": info.max_length,
                # Ask for the format the element actually parses, so the common
                # case needs no normalising at all.
                "format": NATIVE_FORMATS.get(info.input_type),
            },
        )

    @staticmethod
    def _read_back(target, info) -> str | None:
        """
        What the field shows now. None when the element has no readable value.
        """
        if info.tag in ("input", "textarea"):
            try:
                return target.input_value()
            except PlaywrightError:
                return None
        return None

    def _recover(self, job: str, assignment: Assignment, error: str | None) -> FillReport:
        """
        The locator missed. Hand it to `recovery`, if one was given.

        Only for locator failures — never for a rejected value, which is the
        page talking and not something a model can fix by looking harder.
        """
        failed = FillReport(ok=False, fieldId=assignment.fieldId, errorClass=error)
        if self.recovery is None:
            logger.warning(
                "[%s] %s on %s (%s); no recovery configured",
                job, error, assignment.fieldId, assignment.locator,
            )
            return failed

        logger.info("[%s] %s on %s; attempting recovery", job, error, assignment.fieldId)
        return self.recovery.attempt(job, assignment, failed, self)


def _kept(typed: str, shown: str) -> bool:
    """
    Did the field keep what was typed?

    Not a string comparison, because a masked field reformats as you type. The
    live Pie page stores a FEIN of "12-3456789" as "123456789" and shows a
    premium of "1200" as "1,200"; comparing literally reads both as refusals
    and loses two real fields on the first page alone.

    What still has to be caught is a field that DROPS content — a five-digit
    ZIP given six digits, an input that clears itself — so only formatting
    characters are forgiven and everything else must survive intact.
    """
    if shown == typed:
        return True
    significant = _FORMATTING.sub("", typed)
    return bool(significant) and _FORMATTING.sub("", shown) == significant


def _to_native(value: str, input_type: str) -> str | None:
    """
    Coerce a human-written value into what a native input parses.

    Returns None when it cannot be read as a date/time at all, which is a
    rejected value rather than something to retry.
    """
    text = value.strip()
    if input_type != "date":
        # month/time/datetime-local are rarer; accept them only already-shaped.
        shapes = {
            "month": r"\d{4}-\d{2}",
            "time": r"\d{2}:\d{2}",
            "datetime-local": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
        }
        return text if re.fullmatch(shapes[input_type], text) else None

    for fmt in _DATE_ORDERS:
        try:
            return datetime.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None
