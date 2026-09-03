# Form filler run report

Branch: `feat/form-filler`, from `feat/scraper` at `8c7b2e4`.

## What was built

`fill_one(page, assignment, settings, ledger) -> FillReport`. One Assignment in,
one FillReport out. Sync Playwright, matching the scraper. It never chooses the
next assignment and never constructs or repairs a locator.

Intents handled:

- `fill` — types the assignment's value; when the assignment carries none, one
  LLM call chooses it. The field's label is read off the live control, because
  the Assignment carries only `fieldId` (`q_003`), which is useless for judgment.
- `select` — clicks `optionLocator` when set, otherwise `select_option` against
  the parent, by label with a fallback to the option's value.
- `check` — clicks the control and reports the state it ended in.
- `expand` — opens the widget, reads the options that mount, closes it with
  Escape without selecting, and reports `optionsRevealed`. A native `<select>`
  never mounts a listbox, so its own `<option>` elements are read instead.
- `advance` — clicks after the denylist check, then waits for networkidle,
  tolerating a page that never settles.

The retry: after a fill, `aria-invalid="true"` on the control is the rejection
signal, checked first and alone. Error text is hunted separately via
`aria-describedby`/`aria-errormessage` and then a three-level sweep of the
field's container. At most two corrections; a field still invalid after that is
a blocked report with the error text in `whatYouTried`. `constraint` is reported
as `{unit, format, hint}` where `format` is a mask of the value that was
accepted (`123456789` -> `999999999`) and `hint` is the page's words at the
moment of rejection, carried forward because the message is gone once the field
is accepted.

The `expand` invariant: URL and every form field's value are captured before the
open and compared after. Either moving means the target was not a disclosure,
and the report is blocked rather than carrying options from a page that changed.
The element itself is checked for `aria-expanded`, a `combobox`/`listbox` role,
a native `<select>`, or `aria-controls` pointing at a listbox before the click.

Credentials: `$EMAIL` and `$PASSWORD` resolve from `resolve_carrier_creds()` at
the moment of typing. `valueUsed` reports the placeholder. `$OTP` is a blocked
report — a one-time code is not in the credential store, and typing an empty
string would submit a login the run then reports as succeeded.

The denylist (`agents/form_filler/safety.py`) runs inside `write_tools.click`,
on the resolved element, before dispatch. Three signals: denied phrases matched
word-wise against the element's aria-label, text, value, title and name;
enclosure in an ancestor form/section/dialog/fieldset whose identity reads as
payment/checkout/billing; and a `cc-*` autocomplete token or card-number/CVV
identifier on any field in the same form. A Playwright failure while checking is
itself a denial — "could not tell" never resolves to "safe" for an irreversible
action. Refusal raises `RefusedError`, which becomes a blocked report.

Ledger: every call to `fill_one` records one step with `agent="form_filler"`,
`action` = the intent, `detail` = the fieldId or locator, `ms`, `ok`, and `usd`
from the `CostTracker` passed to the value-choosing call. A call that could not
be priced sets `unpriced=True` rather than reporting a made-up number.

## Files touched

Added:

- `src/trailblazer/agents/browser/write_tools.py`
- `src/trailblazer/agents/form_filler/safety.py`
- `src/trailblazer/agents/form_filler/values.py`
- `src/trailblazer/prompts/form_filler/system.md`
- `tests/fixtures/fill.html`
- `tests/test_form_filler.py`

Modified:

- `src/trailblazer/agents/form_filler/form_filler.py` (was an empty tracked stub)

Not touched: `contracts/`, `agents/scraper/`, `agents/frontier/`,
`agents/replay_gen/`, `agents/validator/`, `.gitignore`, and everything in
`.sessions/` other than this file.

## Test results

`uv run pytest tests/ -q`:

```
        response = client.post("/v0/carriers/pie/crawl", json=PAYLOAD)

>       assert response.status_code == 500
E       assert 400 == 500
E        +  where 400 = <Response [400 Bad Request]>.status_code

tests/test_api.py:151: AssertionError
=========================== short test summary info ============================
FAILED tests/test_api.py::test_crawl_returns_the_scraper_result - assert 400 ...
FAILED tests/test_api.py::test_a_crawl_failure_is_a_500_with_the_cause_in_detail
2 failed, 84 passed in 105.22s (0:01:45)
```

The two failures are pre-existing and environmental, not caused by this branch.
`test_api.py` calls `resolve_carrier_creds()`, which raises when `CARRIER_URL` is
unset; the endpoint turns that into a 400 before `run_crawl` is reached. `.env`
is gitignored and absent from this worktree. The same two tests fail identically
on `feat/scraper` before any change from this branch:

```
FAILED tests/test_api.py::test_crawl_returns_the_scraper_result - assert 400 ...
FAILED tests/test_api.py::test_a_crawl_failure_is_a_500_with_the_cause_in_detail
2 failed, 50 passed in 15.51s
```

With the variable set, `CARRIER_URL=https://example.com/form uv run pytest tests/ -q`:

```
........................................................................ [ 83%]
..............                                                           [100%]
86 passed in 105.86s (0:01:45)
```

86 = 52 pre-existing + 34 new. New tests, in `tests/test_form_filler.py`:

```
tests/test_form_filler.py::test_fill_types_the_assignment_value_and_reports_it
tests/test_form_filler.py::test_fill_with_no_value_asks_for_one_and_reports_what_it_typed
tests/test_form_filler.py::test_select_on_a_radio_clicks_the_option_locator
tests/test_form_filler.py::test_select_on_a_native_select_sets_it_by_label
tests/test_form_filler.py::test_select_by_label_falls_back_to_the_option_value
tests/test_form_filler.py::test_select_with_an_unknown_label_is_blocked_not_raised
tests/test_form_filler.py::test_check_clicks_the_control_and_reports_the_state
tests/test_form_filler.py::test_expand_reads_the_options_and_leaves_the_widget_closed
tests/test_form_filler.py::test_expand_does_not_select_anything
tests/test_form_filler.py::test_expand_refuses_a_control_that_is_not_a_disclosure
tests/test_form_filler.py::test_expand_reads_a_native_selects_own_options
tests/test_form_filler.py::test_a_rejected_fill_is_retried_and_the_constraint_is_reported
tests/test_form_filler.py::test_the_correction_call_is_given_the_pages_error_text
tests/test_form_filler.py::test_an_accepted_fill_reports_no_constraint_and_no_retry
tests/test_form_filler.py::test_a_field_still_rejected_after_two_retries_is_blocked
tests/test_form_filler.py::test_a_denylisted_button_is_refused_and_never_clicked[purchase-policy]
tests/test_form_filler.py::test_a_denylisted_button_is_refused_and_never_clicked[bind-coverage]
tests/test_form_filler.py::test_an_innocuous_button_inside_a_payment_form_is_refused
tests/test_form_filler.py::test_an_ordinary_button_is_not_refused
tests/test_form_filler.py::test_advance_clicks_an_allowed_button
tests/test_form_filler.py::test_an_unreadable_element_is_a_denial_not_a_pass
tests/test_form_filler.py::test_a_password_is_typed_but_the_report_carries_the_placeholder
tests/test_form_filler.py::test_an_email_placeholder_resolves_to_the_configured_username
tests/test_form_filler.py::test_a_credential_never_reaches_the_log
tests/test_form_filler.py::test_an_otp_is_blocked_because_nothing_stores_one
tests/test_form_filler.py::test_an_unresolvable_locator_gives_a_blocked_report_not_an_exception
tests/test_form_filler.py::test_a_non_unique_locator_is_blocked_rather_than_acting_on_the_first_match
tests/test_form_filler.py::test_every_step_is_recorded_on_the_ledger
tests/test_form_filler.py::test_a_step_with_no_llm_call_records_no_cost
tests/test_form_filler.py::test_shape_of_records_the_format_the_page_accepted[123456789-999999999]
tests/test_form_filler.py::test_shape_of_records_the_format_the_page_accepted[12-3456789-99-9999999]
tests/test_form_filler.py::test_shape_of_records_the_format_the_page_accepted[2026-01-31-9999-99-99]
tests/test_form_filler.py::test_shape_of_records_the_format_the_page_accepted[CA-AA]
tests/test_form_filler.py::test_shape_of_records_the_format_the_page_accepted[(555) 123-4567-(999) 999-9999]
```

All against `tests/fixtures/fill.html` over `file://`, in `BrowserSession` on CDP
port 9330. No live carrier portal was contacted. `choose_value` is patched in
every test that reaches it, so no API key is required and no LLM call was made.

## Contract limitations hit

1. **`Assignment` carries no label.** `fieldId` is `q_003`, which is not
   something a model can judge a value from. Worked around by reading the
   control's accessible name off the live page (`_label_of`). This costs an
   extra DOM evaluation per unvalued fill and fails on a control with no label
   at all, where the fieldId is used and the chosen value will be poor. The
   contract was not edited.

2. **`FillReport.constraint` is `dict[str, str]`, not a typed model.** `{unit,
   format, hint}` is documented in the docstring only, so nothing validates that
   the three keys are present or that no fourth is added. Filled exactly as
   documented; not enforceable from here.

3. **`Assignment` and `FillReport` are not re-exported from
   `contracts/__init__.py`.** Imported from `contracts.assignment` directly.
   Adding the re-export would have meant editing `contracts/`.

4. **No `Intent` for "uncheck".** `check` toggles, so a checkbox already checked
   is unchecked by a `check` assignment. The report says which state it ended
   in, so the caller can tell, but the assignment cannot request a specific one.

## Not done, and why

- **The final-submit-is-the-bind case is not detected.** A portal whose last
  application submit *is* the bind, with no separate button, is indistinguishable
  from an ordinary "Submit" by name, by enclosing form, and by card
  relationship. Section 5 of the spec makes this a stop condition rather than a
  check, and `safety.py` does not guess it. Recorded in that module's docstring.

- **`values.py` is not exercised against a real model.** Every test patches
  `choose_value`. The prompt at `prompts/form_filler/system.md` and the
  first-line trimming in `_first_line` are untested against live output.

- **`wait_settled` on `advance` is not tested against a real navigation.** The
  fixture's buttons do not navigate, so the test asserts the click landed, not
  that the wait behaves on a page transition.

- **`fill_one` is not wired into `loop/orchestrator.py`.** The loop still runs a
  single perceive. Wiring it needs Frontier to emit the Assignment, and Frontier
  is owned by another agent on a parallel branch.
