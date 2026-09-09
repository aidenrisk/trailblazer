"""The form filler: one Assignment in, one FillReport out.

The only agent that changes the page. It does not decide what to act on next --
Frontier holds the board -- and it does not decide where a control lives, since
every locator arrives measured by the scraper. What it does decide is the
*value*: "fill the FEIN field" names no value, and choosing a plausible one is
judgment, so that single decision is the LLM call. "Select Yes" carries its
value already and costs nothing.

Two things it owns that nothing else can:

- **The retry.** A field demanding nine digits states that requirement nowhere
  the filler can see in advance; it surfaces only as page text after the fill is
  rejected. The filler is the agent standing on the page while that text exists,
  so it reads it, corrects, refills and reports the constraint. Routing this to
  Frontier would cost a full scrape-assign cycle to learn something already on
  screen.
- **`expand`.** Opening a `<div role="combobox">` mounts its whole listbox at
  once, which is the only way to enumerate a set whose options are not in the
  DOM. The widget is closed again without a selection, and the page's URL and
  every form value are compared before and after: an open that navigated, or
  that answered a field nobody answered, was not a disclosure, and the report
  says so rather than pretending.
"""

import json
import logging
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.agents.browser import write_tools
from trailblazer.agents.browser.write_tools import LocatorError, RefusedError
from trailblazer.agents.form_filler.values import choose_value
from trailblazer.contracts.assignment import Assignment, FillReport
from trailblazer.observability.events import event
from trailblazer.observability.ledger import RunLedger
from trailblazer.observability.logging import get_logger
from trailblazer.shared.config import Settings, get_settings
from trailblazer.shared.dev_carrier_creds import resolve_carrier_creds

log = get_logger(__name__)

_MAX_RETRIES = 2
"""Refills allowed after a rejection. A field still invalid after two corrections
is reported blocked: a third attempt has never been the one that worked, and the
cost is another LLM call plus another page round trip."""

_CREDENTIAL_PLACEHOLDERS = ("$EMAIL", "$PASSWORD", "$OTP")
"""Assignment values that name a credential instead of carrying one.

The literal is resolved at the moment of typing and never travels: not into the
report, whose `valueUsed` keeps the placeholder, and not into a log line. The
report becomes the metadata artifact's login stage, which is written to disk.
"""

_MARK_JS = """
() => { let i = 0; document.querySelectorAll('*').forEach((n) => { if (!n.__tbSeen) n.__tbSeen = ++i; }); }
"""
"""Stamp every element that exists now, so what an open mounts can be told apart.

Identity, not visibility: a widget that opens by scrolling the page into view
makes dozens of existing elements newly visible, and a visibility diff read
those as options once.
"""

_APPEARED_OPTIONS_JS = """
(el) => {
  const doc = el.ownerDocument;
  // innerText, not textContent: an option laid out as two lines -- a name
  // over a code -- has its lines glued by textContent and separated by the
  // accessibility engine, so a name built from textContent addresses nothing
  // and a label recorded from it is one no client answer could ever equal.
  const text = (n) => ((n.innerText ?? n.textContent) || '').trim().replace(/\\s+/g, ' ');
  const isNew = (n) => !n.__tbSeen;
  const vis = (n) => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const out = [];
  const seen = new Set();
  const add = (n) => {
    const t = text(n);
    if (!t || seen.has(t)) return;
    seen.add(t);
    out.push({ label: t, id: n.id || '', testid: n.getAttribute('data-testid') || '',
               role: n.getAttribute('role') || '' });
  };
  // A native select never mounts anything: its <option> children are the set.
  if (el.tagName.toLowerCase() === 'select') {
    for (const o of el.options) { const t = text(o); if (t && !seen.has(t)) { seen.add(t); out.push({ label: t, id: '', testid: '', role: 'option', native: true }); } }
    return out;
  }
  // A root the control names is authoritative wherever the page put it.
  for (const id of (el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '').split(/\\s+/)) {
    const root = id && doc.getElementById(id);
    if (root) root.querySelectorAll('[role="option"], [role="menuitem"], option').forEach(add);
  }
  if (out.length) return out;
  // Otherwise: exactly the option-shaped elements that did not exist before
  // the open and are visible now. Never a page-wide search -- that returned
  // the portal's nav as a field's options.
  doc.querySelectorAll('[role="option"], [role="menuitem"]').forEach((n) => { if (isNew(n) && vis(n)) add(n); });
  if (!out.length) doc.querySelectorAll('li').forEach((n) => { if (isNew(n) && vis(n)) add(n); });
  return out;
}
"""


_FORM_STATE_JS = """
() => {
  const out = {};
  const fields = document.querySelectorAll('input, select, textarea');
  fields.forEach((e, i) => {
    // Keyed by identity where the field has one, by index otherwise. A blob
    // joined on position cannot say *which* field moved, and that is the whole
    // distinction between a reformat and an answer nobody gave.
    const key = e.id || e.name || `@${i}`;
    out[key] = (e.type === 'checkbox' || e.type === 'radio')
      ? String(e.checked) : String(e.value);
  });
  return out;
}
"""
"""Every form field's value, keyed by id, name, or position as a last resort."""


def _unexpected_writes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Fields that gained a value, lost one, or appeared, while a widget was open.

    A field that was already answered and now reads differently is a formatter
    firing on blur -- `5551234567` becoming `(555) 123-4567` -- which changes
    nothing downstream: the artifact keeps what was typed, and at replay the
    same formatter runs on the same input. Those are not reported.

    A field that was empty and now holds something is a value the walk never
    chose, which will be submitted and which no artifact records. A field that
    lost its value, and a field that appeared or vanished across the click, are
    the same class of surprise. Those are what block the expand.
    """
    changed = [key for key in before if key not in after]
    for key, now in after.items():
        was = before.get(key)
        if was is None or bool(now) != bool(was):
            changed.append(key)
    return sorted(set(changed))


def fill_one(
    page: Page,
    assignment: Assignment,
    settings: Settings | None = None,
    ledger: RunLedger | None = None,
) -> FillReport:
    """Perform one assignment and report what happened.

    Never raises for a page-level failure: an unresolvable locator, a refused
    click and a value the page will not accept all come back as a report with
    `ok=False` and `blocked` filled in, because Loop routes on the report and a
    traceback would take the whole crawl down over one field.
    """
    settings = settings or get_settings()
    started = time.monotonic()
    usd = 0.0
    unpriced = False
    try:
        if assignment.intent == "fill":
            report, usd, unpriced = _do_fill(page, assignment, settings)
        elif assignment.intent == "select":
            report, usd, unpriced = _do_select(page, assignment, settings)
        elif assignment.intent == "check":
            report = _do_check(page, assignment)
        elif assignment.intent == "expand":
            report = _do_expand(page, assignment)
        elif assignment.intent == "advance":
            report = _do_advance(page, assignment)
        else:
            # Unreachable while `Intent` is a Literal, and kept so that widening
            # the enum without widening this function fails loudly here rather
            # than silently reporting success for an action never performed.
            raise RuntimeError(f"no handler for intent {assignment.intent!r}")
    except (LocatorError, RefusedError) as e:
        report = _blocked(assignment, str(e))
    except PlaywrightError as e:
        report = _blocked(assignment, f"the page failed during the action: {e}")

    ms = int((time.monotonic() - started) * 1000)
    if ledger is not None:
        ledger.record(
            agent="form_filler",
            action=assignment.intent,
            detail=assignment.fieldId or assignment.locator,
            usd=usd,
            ms=ms,
            ok=report.ok,
            unpriced=unpriced,
        )
    event(
        "fill", "filler",
        logging.INFO if report.ok else logging.WARNING,
        field=assignment.fieldId,
        label=assignment.label or None,
        intent=assignment.intent,
        locator=assignment.locator,
        value=report.valueUsed,
        ok=report.ok,
        retried=report.retried or None,
        refused=(report.blocked or {}).get("whatYouTried"),
        ms=ms,
        usd=usd or None,
    )
    return report


def _report(assignment: Assignment, **fields) -> FillReport:
    """A FillReport carrying the assignment's identity plus whatever happened."""
    return FillReport(
        fieldId=assignment.fieldId,
        intent=assignment.intent,
        locator=assignment.locator,
        **fields,
    )


def _blocked(assignment: Assignment, what_you_tried: str) -> FillReport:
    """The report for an action that could not be completed. Never an exception."""
    return _report(
        assignment,
        ok=False,
        blocked={
            "control": assignment.fieldId or assignment.locator,
            "whatYouTried": what_you_tried,
        },
    )


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def _resolve_credential(placeholder: str, settings: Settings) -> str:
    """The literal behind `$EMAIL` / `$PASSWORD`, looked up at the moment of typing.

    `$OTP` has no source: the credential store holds a username and a password,
    and a one-time code by definition is not stored. It raises rather than
    typing an empty string, which would submit a login the run then reports as
    having succeeded.
    """
    creds = resolve_carrier_creds("current", settings)
    literal = {"$EMAIL": creds.username, "$PASSWORD": creds.password}.get(placeholder)
    if placeholder == "$OTP":
        raise LocatorError(
            "$OTP cannot be resolved: a one-time code is not in the credential store. "
            "The login must be completed by hand in the shared browser."
        )
    if not literal:
        raise LocatorError(
            f"{placeholder} is not configured for this carrier "
            "(CARRIER_USERNAME / CARRIER_PASSWORD in the dev credential stub)"
        )
    return literal


# --------------------------------------------------------------------------- #
# fill, and the retry it owns
# --------------------------------------------------------------------------- #


def _do_fill(
    page: Page, assignment: Assignment, settings: Settings
) -> tuple[FillReport, float, bool]:
    """Type a value, and correct it if the page rejects it.

    Returns the report alongside the LLM spend, which the caller records on the
    ledger -- the cost belongs to this step and nothing else can see it.
    """
    if assignment.value in _CREDENTIAL_PLACEHOLDERS:
        placeholder = assignment.value
        write_tools.fill(page, assignment.locator, _resolve_credential(placeholder, settings))
        # The placeholder, not the literal: this report is persisted.
        return _report(assignment, ok=True, valueUsed=placeholder), 0.0, False

    usd = 0.0
    unpriced = False
    # The scraper's label first: it is the question as a person reads it, and
    # the model cleaned it from markup the live page does not expose. Read off
    # the control only when the assignment carries none. Pie's eligibility
    # percentages have no accessible name, so the live read returned nothing,
    # the chooser was given `q_002`, and it answered "what percentage of labor
    # cost" with a business name -- five refused presses on one page.
    label = (
        assignment.label
        or _label_of(page, assignment.locator)
        or assignment.fieldId
        or assignment.locator
    )

    if assignment.value is not None:
        value = assignment.value
    else:
        value, usd, unpriced = choose_value(
            label=label,
            locator=assignment.locator,
            url=page.url,
            constraint_hint=assignment.constraintHint,
            error_text=None,
            settings=settings,
            business_type=settings.crawl_business_type,
            state=settings.crawl_state,
            control_type=_type_of(page, assignment.locator),
            help_text=assignment.helpText,
        )

    if assignment.typeahead:
        return _type_and_pick(page, assignment, value, usd, unpriced)

    write_tools.fill(page, assignment.locator, value)

    retried = False
    first_rejection = None
    for _ in range(_MAX_RETRIES):
        error_text = _rejection_text(page, assignment.locator)
        if error_text is None:
            break
        retried = True
        first_rejection = first_rejection or error_text
        value, call_usd, call_unpriced = choose_value(
            label=label,
            locator=assignment.locator,
            url=page.url,
            constraint_hint=assignment.constraintHint,
            error_text=error_text,
            settings=settings,
            business_type=settings.crawl_business_type,
            state=settings.crawl_state,
            control_type=_type_of(page, assignment.locator),
            help_text=assignment.helpText,
        )
        usd += call_usd
        unpriced = unpriced or call_unpriced
        write_tools.fill(page, assignment.locator, value)

    # Re-read after the last correction: the loop exits on its counter as well
    # as on acceptance, so the final state has to be established either way.
    final_error = _rejection_text(page, assignment.locator)
    if final_error is not None:
        return (
            _blocked(
                assignment,
                f"still rejected after {_MAX_RETRIES} corrections: {final_error}",
            ),
            usd,
            unpriced,
        )

    # Recorded whether or not a rejection happened: a field answered correctly
    # first time still knows what the page and its tooltip stated, and that is
    # what the replay script shapes a different client answer against.
    stated = "; ".join(t for t in (assignment.constraintHint, assignment.helpText) if t)
    constraint = _constraint_from(page, assignment.locator, stated, first_rejection or "")
    return (
        _report(assignment, ok=True, valueUsed=value, retried=retried, constraint=constraint),
        usd,
        unpriced,
    )


_SUGGESTION_BUDGET_MS = 5_000
"""How long a typeahead is given to raise its suggestions after typing.

Polled, not slept: Pie's class-code lookup answered in 0.4s when the field was
clicked first and 2.4s when it was not, and a fixed 1.8-second wait saw nothing
and refused to pick. The lookup is a network call; its latency is not ours to
assume."""


def _wait_for_suggestions(page: Page, locator: str) -> list[dict[str, str | None]]:
    """The suggestions a typeahead raised, polled until some appear or the budget ends."""
    deadline = time.monotonic() + _SUGGESTION_BUDGET_MS / 1000
    while True:
        options = _read_appeared_options(page, locator)
        if options or time.monotonic() >= deadline:
            return options
        page.wait_for_timeout(200)


def _type_and_pick(
    page: Page, assignment: Assignment, value: str, usd: float, unpriced: bool
) -> tuple[FillReport, float, bool]:
    """Type into a typeahead and pick the suggestion that matches.

    Pie's class-code box: type "5183" and three suggestions appear, each a
    button reading "Plumbing - Low Wage / 5183"; clicking one sets the field to
    the code. Typed text left unpicked is not a class, so the pick is the
    commit. The match is the suggestion whose text contains what was typed;
    with the class code as the seed that is the code itself, which is unique.
    Several matches with none exact is a real ambiguity and is blocked rather
    than guessed at.
    """
    page.evaluate(_MARK_JS)
    # Clicked first: focus arms the lookup, and the suggestions then arrive in
    # under half a second instead of two and a half.
    write_tools.click(page, assignment.locator)
    write_tools.type_text(page, assignment.locator, value)
    options = _wait_for_suggestions(page, assignment.locator)
    needle = " ".join(value.split()).casefold()
    matches = [o for o in options if needle in " ".join(o["label"].split()).casefold()]
    exact = [o for o in matches if o["label"].casefold().split()[-1:] == [needle]]
    chosen = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
    if chosen is None:
        _close(page, assignment.locator)
        return _blocked(
            assignment,
            f"typeahead offered {len(options)} suggestions for {value!r}, "
            f"{len(matches)} matching; nothing unambiguous to pick",
        ), usd, unpriced
    target = chosen.get("locator") or _by_typed_text(page, value)
    if target is None:
        _close(page, assignment.locator)
        return _blocked(
            assignment,
            f"typeahead matched {chosen['label']!r} for {value!r} but no unique address "
            "could be measured for it",
        ), usd, unpriced
    write_tools.click(page, target)
    page.wait_for_timeout(300)
    committed = write_tools.resolve(page, assignment.locator).input_value() or chosen["label"]
    event(
        "fill", "filler", field=assignment.fieldId, locator=assignment.locator,
        via="typeahead", typed=value, picked=chosen["label"], holds=committed,
    )
    stated = "; ".join(t for t in (assignment.constraintHint, assignment.helpText) if t)
    constraint = _constraint_from(page, assignment.locator, stated, "")
    return _report(assignment, ok=True, valueUsed=committed, constraint=constraint), usd, unpriced


def _by_typed_text(page: Page, value: str) -> str | None:
    """Address a suggestion by the text that was typed, when its full name will not.

    A suggestion's accessible name is the role engine's to compute -- Pie's
    class-code buttons split name and code across two lines and the computed
    name did not equal the text as read -- so a name-based address can miss.
    What was typed is unique among the suggestions it raised, by construction:
    the lookup returned them for it. Measured unique like every other locator.
    """
    quoted = json.dumps(value)
    for sel in (f'[role="option"]:has-text({quoted}) >> visible=true',
                f'[role="menuitem"]:has-text({quoted}) >> visible=true'):
        try:
            if page.locator(sel).count() == 1:
                return sel
        except PlaywrightError:
            continue
    return None


_LABEL_JS = """
(el) => {
  const byAria = el.getAttribute('aria-label');
  if (byAria) return byAria.trim();
  const labelledBy = el.getAttribute('aria-labelledby');
  if (labelledBy) {
    const n = el.ownerDocument.getElementById(labelledBy.split(/\\s+/)[0]);
    if (n && n.textContent.trim()) return n.textContent.trim();
  }
  if (el.id) {
    const n = el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (n && n.textContent.trim()) return n.textContent.trim();
  }
  const wrapping = el.closest('label');
  if (wrapping && wrapping.textContent.trim()) return wrapping.textContent.trim();
  return el.getAttribute('placeholder') || el.getAttribute('name') || null;
}
"""


def _label_of(page: Page, locator: str) -> str | None:
    """The control's human-readable name, read off the live element.

    A missing label is not an error here: the caller falls back to the fieldId,
    and a model given a poor name chooses a poorer value rather than failing.
    """
    try:
        return write_tools.resolve(page, locator).evaluate(_LABEL_JS)
    except (PlaywrightError, LocatorError):
        return None


def _type_of(page: Page, locator: str) -> str:
    """The control's own type, read off the live element.

    `<input type="date">` and `<input type="tel">` state the shape they want
    without any placeholder or pattern, which Pie's inputs do not carry. Read
    here rather than taken from `Control.type`, whose five-value enum is the
    model's normalisation and collapses `date`, `tel` and `email` detail away.
    """
    try:
        return write_tools.resolve(page, locator).evaluate(_TYPE_JS) or ""
    except (PlaywrightError, LocatorError):
        return ""


_TYPE_JS = """
(el) => {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  return type && tag === 'input' ? `input[type=${type}]` : tag;
}
"""


_PAGE_PROBLEMS_JS = """
() => {
  const out = [];
  const vis = (n) => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const text = (n) => (n && (n.textContent || '').trim()) || '';
  document.querySelectorAll('input, select, textarea').forEach((el) => {
    if (!vis(el) || el.disabled) return;
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button') return;
    const id = el.id ? '#' + CSS.escape(el.id) : (el.name ? `[name="${el.name}"]` : '');
    let message = '';
    for (const sid of (el.getAttribute('aria-errormessage') || '').split(/\\s+/)) {
      const t = sid && text(document.getElementById(sid)); if (t) { message = t; break; }
    }
    const invalid = (el.getAttribute('aria-invalid') || '').toLowerCase() === 'true';
    if (invalid || message) out.push({locator: id, problem: message || 'marked invalid'});
  });
  return out;
}
"""


def page_problems(page: Page) -> list[dict[str, str]]:
    """Fields the page itself marks rejected, read straight off the DOM.

    No model, and no guessing: only a control carrying `aria-invalid` or text
    in its named error slot is reported. Whether an empty field is required is
    the page's judgment, made when Next is pressed. Run after a forward press
    that was refused, so a rejected field is re-filled rather than the page
    being declared done.
    """
    try:
        return page.evaluate(_PAGE_PROBLEMS_JS)
    except PlaywrightError as e:
        log.warning("could not read the page for problems: %s", e)
        return []


def _rejection_text(page: Page, locator: str) -> str | None:
    """The error the page is showing for this control, or None if it accepted it.

    Three signals, any one of which is a rejection: the field's `aria-invalid`
    flag; text in the element the field names as its error slot; or error-styled
    text beside the field. One attribute alone was the earlier rule and it
    misses a portal that prints a red message without setting the flag.

    `aria-errormessage` is trusted unconditionally -- its purpose is errors.
    `aria-describedby` is not: on Pie it points at "Maximum 250 characters",
    which is help, so it counts only when the target is an alert or styled as
    an error. A flag with no message is still a rejection; the returned text
    then says only that.
    """
    try:
        found = write_tools.resolve(page, locator).evaluate(_REJECTION_JS)
    except (PlaywrightError, LocatorError):
        return None
    invalid, message = bool(found.get("invalid")), (found.get("message") or "").strip()
    if not invalid and not message:
        return None
    return message or "the field is marked aria-invalid and the page gave no message"


_REJECTION_JS = """
(el) => {
  const doc = el.ownerDocument;
  const isErrorish = (n) =>
    n && ((n.getAttribute('role') || '') === 'alert' ||
          /error|invalid|danger/i.test(n.getAttribute('class') || ''));
  const textOf = (n) => (n && (n.textContent || '').trim()) || '';

  let message = '';
  for (const id of (el.getAttribute('aria-errormessage') || '').split(/\\s+/)) {
    const t = id && textOf(doc.getElementById(id));
    if (t) { message = t; break; }
  }
  if (!message) {
    for (const id of (el.getAttribute('aria-describedby') || '').split(/\\s+/)) {
      const n = id && doc.getElementById(id);
      const t = isErrorish(n) ? textOf(n) : '';
      if (t) { message = t; break; }
    }
  }
  if (!message) {
    let scope = el.parentElement;
    for (let i = 0; i < 3 && scope && !message; i++, scope = scope.parentElement) {
      const nodes = scope.querySelectorAll(
        '[role="alert"], [aria-live], .error, .invalid, [class*="error" i], [class*="invalid" i]');
      for (const n of nodes) {
        if (n === el || n.contains(el)) continue;
        const t = textOf(n);
        if (t) { message = t; break; }
      }
    }
  }
  return { invalid: (el.getAttribute('aria-invalid') || '').toLowerCase() === 'true', message };
}
"""


def _constraint_from(
    page: Page, locator: str, stated: str, rejection: str
) -> dict[str, str] | None:
    """`{unit, format, hint}` for a field that has just been accepted, or None.

    `stated` is what the page and its tooltip said before any attempt; `rejection`
    is the page's complaint when one happened. Both reach `hint`, because the
    rejection alone often states no rule -- "Please enter the FEIN" -- and the
    tooltip alone does not say the field enforces it.

    `format` is the shape of the accepted value, measured from the control, and
    recorded only when the value is digit-shaped: `842673915` gives `999999999`
    and `04/15/2027` gives `99/99/9999`, both real rules the replay script can
    shape a different answer to. Free text has no shape worth recording.
    """
    try:
        element = write_tools.resolve(page, locator)
        accepted = element.input_value()
        unit = element.get_attribute("data-unit") or ""
    except (PlaywrightError, LocatorError):
        accepted, unit = "", ""

    fmt = _shape_of(accepted) if _is_digit_shaped(accepted) else ""
    hint = "; ".join(t for t in (stated, f"rejected with: {rejection}" if rejection else "") if t)
    if not (unit or fmt or hint):
        return None
    return {"unit": unit, "format": fmt, "hint": hint}


def _is_digit_shaped(value: str) -> bool:
    """True when `value` is digits with at most separators -- a FEIN, ZIP, phone, date."""
    return any(c.isdigit() for c in value) and not any(c.isalpha() for c in value)


def _shape_of(value: str) -> str:
    """A value's format as a mask: digits to `9`, letters to `A`, punctuation kept.

    `123456789` becomes `999999999`, `12-3456789` becomes `99-9999999`. Only a
    digit-shaped value's mask is recorded (`_is_digit_shaped`); this reaches the
    questions artifact and the replay script shapes a client's answer to it.
    """
    return "".join("9" if c.isdigit() else "A" if c.isalpha() else c for c in value)


# --------------------------------------------------------------------------- #
# select, check, advance
# --------------------------------------------------------------------------- #


def _do_select(
    page: Page, assignment: Assignment, settings: Settings | None = None
) -> tuple[FillReport, float, bool]:
    """Set a choice: click the option's own locator, or set the parent by label.

    A radio's choices are separate clickable inputs and carry their own
    `optionLocator`; a native `<select>`'s do not, and are set by label against
    the parent.

    A select with no value is a non-gate dropdown Frontier left to the filler:
    the model picks one of the listed labels -- the plausible entity type for
    the business, not the first option, which is often a placeholder -- and the
    chosen label's locator is what gets clicked. Returns the report and the
    call's cost, like `_do_fill`.
    """
    usd, unpriced = 0.0, False
    if assignment.value is None and assignment.options:
        settings = settings or get_settings()
        label = _label_of(page, assignment.locator) or assignment.fieldId or assignment.locator
        labels = [o["label"] for o in assignment.options if o.get("label")]
        chosen, usd, unpriced = choose_value(
            label=label,
            locator=assignment.locator,
            url=page.url,
            constraint_hint=assignment.constraintHint,
            error_text=None,
            settings=settings,
            business_type=settings.crawl_business_type,
            state=settings.crawl_state,
            control_type=_type_of(page, assignment.locator),
            help_text=assignment.helpText,
            options=labels,
        )
        if chosen not in labels:
            return _blocked(
                assignment, f"the model chose {chosen!r}, which is not one of {labels}"
            ), usd, unpriced
        by_label = {o["label"]: o.get("locator") for o in assignment.options}
        assignment = assignment.model_copy(
            update={"value": chosen, "optionLocator": by_label.get(chosen)}
        )
    elif assignment.value is not None and not assignment.optionLocator and assignment.options:
        # A pinned re-execution: the value was chosen on the first pass and kept,
        # the locator that pass resolved was not. Looked up again by label, or a
        # custom listbox falls through to the native-select path and blocks.
        by_label = {o["label"]: o.get("locator") for o in assignment.options}
        if by_label.get(assignment.value):
            assignment = assignment.model_copy(update={"optionLocator": by_label[assignment.value]})

    if assignment.optionLocator:
        # A radio's option is always in the DOM. A custom listbox's is mounted
        # only while the widget is open, so if it does not resolve the widget
        # is opened first and the option clicked while it is showing.
        if page.locator(assignment.optionLocator).count() == 0:
            _open_widget(page, assignment.locator)
        write_tools.click(page, assignment.optionLocator)
        page.wait_for_timeout(150)
        return _report(assignment, ok=True, valueUsed=assignment.value), usd, unpriced

    if assignment.value is None:
        return _blocked(assignment, "select with neither an optionLocator nor a value"), usd, unpriced

    used = write_tools.select_option(page, assignment.locator, assignment.value)
    return _report(assignment, ok=True, valueUsed=used), usd, unpriced


def _do_check(page: Page, assignment: Assignment) -> FillReport:
    """Toggle a checkbox and report the state it ended in."""
    write_tools.click(page, assignment.locator)
    try:
        checked = write_tools.resolve(page, assignment.locator).is_checked()
    except PlaywrightError:
        # A `role="switch"` div is not a checkbox and has no checked state; the
        # click still happened, so the action is reported without a value rather
        # than as a failure.
        return _report(assignment, ok=True, valueUsed=None)
    return _report(assignment, ok=True, valueUsed="true" if checked else "false")


def _do_advance(page: Page, assignment: Assignment) -> FillReport:
    """Click a control that moves the page on, then wait for it to settle.

    `write_tools.click` runs the denylist check before dispatch, so a "Purchase"
    or "Bind" button never reaches the page from here.
    """
    before = page.url
    write_tools.click(page, assignment.locator)
    moved = write_tools.wait_for_transition(page, before)
    if moved:
        # The new page may still be loading; a look now describes a spinner.
        write_tools.wait_for_content(page)
    event(
        "advance", "filler",
        logging.INFO if moved else logging.WARNING,
        locator=assignment.locator, was=before, now=page.url if moved else None, moved=moved,
    )
    if not moved:
        # The click happened and the page stayed: the action did not yield its
        # result, so it is not reported as done. Recorded as done, the board
        # marked Next pressed and never pressed it again after the page's
        # fields were repaired.
        return _report(
            assignment, ok=False,
            blocked={"control": assignment.locator, "whatYouTried": "pressed; the page did not move"},
        )
    return _report(assignment, ok=True)


# --------------------------------------------------------------------------- #
# expand
# --------------------------------------------------------------------------- #


def _do_expand(page: Page, assignment: Assignment) -> FillReport:
    """Open a widget, read the options that mount, and close it without selecting.

    A `<div role="combobox">` mounts its listbox only on click, so a read-only
    extraction sees `options: null` and selecting one value at a time enumerates
    the set one draw at a time with no way to know its size. Opening mounts the
    whole set at once.

    The page must be left as it was found, and the assertion for that is not the
    absence of a click: the URL and every form field's value are captured before
    the open and compared after, field by field. A disclosure navigates nowhere
    and answers nothing. A field that merely reads differently is a formatter
    firing on blur and is allowed; a field that gained an answer, lost one, or
    appeared is a value the walk never chose, and the report is blocked rather
    than carrying options gathered from a page that has silently changed.
    """
    before_url = page.url
    before_state = page.evaluate(_FORM_STATE_JS)

    element = write_tools.resolve(page, assignment.locator)
    if not _is_disclosure(element):
        return _blocked(
            assignment,
            "the element is not a disclosure: it has no aria-expanded, no listbox "
            "relationship, and is not a native select",
        )

    page.evaluate(_MARK_JS)
    _open_widget(page, assignment.locator)
    try:
        options = _read_appeared_options(page, assignment.locator)
    finally:
        _close(page, assignment.locator)

    after_state = page.evaluate(_FORM_STATE_JS)
    if page.url != before_url:
        return _blocked(
            assignment,
            f"opening the widget navigated from {before_url} to {page.url}; "
            "the page is compromised and the options read from it are not trustworthy",
        )
    written = _unexpected_writes(before_state, after_state)
    if written:
        return _blocked(
            assignment,
            f"opening the widget wrote to {', '.join(written)}; the target was not a "
            "disclosure and the options read from it are not trustworthy",
        )

    if not options:
        event("fill", "filler", logging.WARNING, field=assignment.fieldId,
              locator=assignment.locator, via="expand", options=0)
    return _report(assignment, ok=True, optionsRevealed=options)


_DISCLOSURE_JS = """
(el) => {
  if (el.tagName.toLowerCase() === 'select') return true;
  if (el.hasAttribute('aria-expanded')) return true;
  const role = (el.getAttribute('role') || '').toLowerCase();
  if (role === 'combobox' || role === 'listbox') return true;
  const controlled = el.getAttribute('aria-controls');
  if (!controlled) return false;
  return controlled.split(/\\s+/).some((id) => {
    const n = el.ownerDocument.getElementById(id);
    const r = n && (n.getAttribute('role') || '').toLowerCase();
    return r === 'listbox' || r === 'menu';
  });
}
"""


def _is_disclosure(element) -> bool:
    """True when the resolved element opens something rather than submitting.

    Checked on the element, not on the name: a function that takes a locator and
    dispatches a click is a general click tool no matter what it is called, so
    the constraint has to live here. Two gaps this cannot close, both covered by
    the before/after comparison in `_do_expand`: a submit control that carries
    `aria-expanded` passes, and the widget's own open handler can do anything.
    """
    try:
        return bool(element.evaluate(_DISCLOSURE_JS))
    except PlaywrightError:
        return False


def _option_locator(page: Page, item: dict) -> str | None:
    """A unique address for one mounted option, or None.

    `#id` and `[data-testid]` when the option has one. Pie's have neither, so
    the address is the role plus the option's own text, and it is measured
    unique while the widget is open like every other locator in the pipeline.
    """
    if item.get("native"):
        return None  # set by label against the select; an <option> is not clicked
    label = item["label"]
    candidates = []
    if item.get("id"):
        candidates.append(f"#{item['id']}")
    if item.get("testid"):
        candidates.append(f'[data-testid="{item["testid"]}"]')
    role = item.get("role") or "option"
    # The role engine resolves to the element carrying the role, with an exact
    # accessible name: `:text-is` binds to the innermost text node -- the <p>
    # inside Pie's option button -- and never matches the button. Visibility
    # is required because a native <select>'s <option> elsewhere on the page
    # carries the same role and name while drawn nowhere.
    quoted = label.replace('"', '\\"')
    candidates.append(f'role={role}[name="{quoted}"] >> visible=true')
    for sel in candidates:
        try:
            if page.locator(sel).count() == 1:
                return sel
        except PlaywrightError:
            continue
    log.debug("no unique locator for option %r", label)
    return None


def _open_widget(page: Page, locator: str) -> None:
    """Open a custom chooser so its options mount.

    A click on the control is what opens Pie's; `mousedown` alone does not. The
    denylist runs on the control as on any click.
    """
    write_tools.click(page, locator)
    page.wait_for_timeout(_OPEN_SETTLE_MS)


_OPEN_SETTLE_MS = 500
"""How long a chooser is given to mount its options after the opening click."""


def _read_appeared_options(page: Page, locator: str) -> list[dict[str, str | None]]:
    """The options the widget mounted, each with the locator that addresses it.

    Called with the widget open and the pre-open element set already stamped by
    `_MARK_JS`. What is returned is what appeared, or what the control's own
    `aria-controls` root holds; nothing else on the page qualifies.
    """
    element = write_tools.resolve(page, locator)
    try:
        items = element.evaluate(_APPEARED_OPTIONS_JS)
    except PlaywrightError as e:
        raise LocatorError(f"could not read the options at {locator!r}: {e}") from e
    return [
        {"label": it["label"], "locator": _option_locator(page, it)}
        for it in items if it.get("label")
    ]


def _close(page: Page, locator: str) -> None:
    """Close the widget without choosing anything.

    Escape first, because it is the one gesture every combobox implementation
    treats as dismiss-without-commit. Enter or a click on an option would select
    the highlighted entry, which is exactly what `expand` must not do.
    """
    try:
        page.keyboard.press("Escape")
        element = write_tools.resolve(page, locator)
        if (element.get_attribute("aria-expanded") or "").lower() == "true":
            # Escape was ignored; a second click on the control itself toggles
            # the same widget shut and still commits nothing. Routed through
            # `write_tools.click` like every other dispatch, so no click in this
            # module reaches the page without the denylist.
            write_tools.click(page, locator)
    except (PlaywrightError, LocatorError, RefusedError) as e:
        log.warning("could not confirm the widget at %s closed: %s", locator, e)
