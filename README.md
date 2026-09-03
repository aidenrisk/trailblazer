# Trailblazer — FormFiller

The agent that touches the form.

```
Assignment  ->  FormFiller  ->  FillReport
```

Built and tested alone on this branch, the way Frontier and Scraper were. Nothing
here imports Loop, Frontier or Scraper; the tests hand `execute()` one Assignment
and check the DOM for what the report claims. Integration comes later.

## What it is for

FormFiller is the only agent holding a live element, which makes it the answer to
two questions nobody else can answer.

**What to type.** Frontier's `Assignment` names a control and carries no value —
Frontier never sees the element. FormFiller does, so FormFiller decides, and
reports the literal back on `FillReport.steps[].value`. That is what a walk slice
records and what a replay eventually types.

**What this control actually is.** Scraper reports `options: null` for any widget
that doesn't render its list until it is opened. FormFiller opens it and reports
what it found on `FillReport.discoveredOptions`. That is the whole discovery
channel; Frontier then walks every option one at a time.

## Deciding the value

`agents/form_filler/value_picker.py`. This is the one genuine judgment call in a
walk, and it is a model, not a rule table.

The rule table it replaces — `shared/values.py`, kept as the offline fallback —
works by sniffing the label for substrings somebody already wrote a branch for:
`"zip"`, `"fein"`, `"email"`. Everything else falls through to `"Test Value"`,
which a real form rejects:

| Label | Rule table | Model |
|---|---|---|
| Business Zip Code | `10001` | `10013` |
| FEIN | `12-3456789` | `12-3456789` |
| **Suite / Unit** | `Test Value` | `4B` |
| **NAICS Code** | `Test Value` | `722511` |
| **Number of Members** | `Test Value` | `3` |

The model is given the label, the input type read off the element, the page's own
heading (so "Name" on a Business Information page means the business's), and the
constraints only a live element carries — `pattern`, `maxLength`, `placeholder`,
and the format a native input demands. Answers are cached on `(label, type)`,
because a chooser is re-filled once per option walked and a multi-page form
repeats fields like Zip on every page.

Everything else stays deterministic. The Assignment already carries an exact
locator, and for a `set_option` the option's own locator too, so "click this" is
a call and not a decision. A model on that path would only paper over locator
bugs that ought to surface.

## The mechanics

| Assignment | What happens |
|---|---|
| `fill_field` on a plain input | Type the value, then read it back. A field that silently drops what was typed is `validation`, not success. |
| `fill_field` on a `<select>` | It was never a field. Report every option with its own `:text-is()` locator, take the first so the page is left in a real state. |
| `fill_field` on a custom widget | Click it open, read `[role=option]`, report them, click the first. The case only the filler can resolve. |
| `fill_field` on a checkbox | Check it. `discoveredOptions` stays `None`. |
| `set_option`, native `<select>` | `select_option(label=...)` on the parent — a native `<option>` cannot be clicked. |
| `set_option`, anything else | Click `option.locator`; open the parent first only if the option isn't on the page yet. |
| `next` / `back` / `submit` | Click, then decide `advance` on evidence — a changed URL — never on intent. |

Two distinctions carry real weight:

- **`discoveredOptions`: `None` vs `[]`.** `None` means "not a chooser, leave what
  you know alone". `[]` means "opened it, it genuinely has none". A zero-option
  chooser reported as `None` is never marked explored and blocks every control
  after it on the page.
- **`advance` is measured, not assumed.** A Loop keys its page counter off this
  flag, and the counter feeds the stage id Frontier keys its board on. A click
  that claimed success while the page stood still would rename the stage and lose
  the board.

## Recovery

`agents/form_filler/recovery.py`. One model attempt to rescue an assignment whose
**locator** missed — `not_found`, `not_unique`, `widget`. Never a rejected value:
that is the page talking, and looking harder will not change its mind.

What comes back is not believed. The filler re-reads the DOM and reports success
only if the value or option is actually there. A model saying it clicked
something is a claim; the element is the evidence.

Recovery is constructor-injected and `None` by default, so a filler built without
one is provably offline.

## Running it

```powershell
# The tests. 33 browser tests + 21 value-picker tests.
# All offline except the live-model class, which skips without a provider key.
.venv\Scripts\python.exe -m pytest -q

# Walk the fixture form and log every Assignment and FillReport as JSON.
$env:PYTHONPATH = "."; .venv\Scripts\python.exe scripts\demo_form_filler.py
$env:PYTHONPATH = "."; .venv\Scripts\python.exe scripts\demo_form_filler.py --rules    # no model, free
$env:PYTHONPATH = "."; .venv\Scripts\python.exe scripts\demo_form_filler.py --headed   # watch it work
```

The demo ends by reading the form back off the page, so the reports and the DOM
can be compared directly:

```
  WHAT THE PAGE HOLDS NOW
  agencyProgram        Pie Partner Program
  effectiveDate        2024-11-01
  businessZipCode      10013
  businessName         Harbor Point Bistro
  entityType           corp
  fein                 12-3456789
  targetPremium        8500
  priorCoverage        true

  13 assignments, 12 ok, 1 failed
  failed: fill_field q_404 -> not_found      <- a deliberately wrong locator
```

## Tests

Real headless Chromium against `tests/fixtures/form_page.html`, not a stand-in
for Playwright's `Page`. The things worth proving are that a fill lands in the
DOM, that a `<select>` really reports its options, and that a widget rendering
nothing until clicked is found by clicking it — a fake Page proves only that the
filler called the methods its author expected.

The fixture carries one of each shape the live Pie page has: a combobox that
renders `[role=option]` only after a click, a native `<select>` with a
placeholder, a split Yes/No pair with no shared parent, a field revealed by a
choice, a five-digit-only input that rejects what it is given, two elements
sharing one locator, and a Next that navigates beside one that doesn't.

## Not here yet

- **Loop wiring.** `execute()` takes `page_description` because
  `Assignment(type="next")` carries no locator; Loop will pass `current_page`.
  `StubFormFiller` needs the same keyword so both fillers share one signature.
- **Applicant data.** Values are realistic but invented. `WalkStep.canonical` and
  `credentialKey` are where real data plugs in, and the picker is the seam.
- **`shared/llm.py` was fixed here**, not copied clean: it imported
  `langchain_openai` at module scope, which is not installed, so the module could
  not load at all. It had no callers before this branch, so nothing had noticed.
  Provider imports are now inside their branches.
