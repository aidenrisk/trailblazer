# Frontier run report

Branch `feat/frontier`, cut from `feat/scraper` at `8c7b2e4`.

## What was built

`Frontier` holds one `Board` per page and answers `next_assignment()` with a
single `Assignment`, or `None` when the page is done. It makes no LLM call and
never touches the browser.

Decision order in `next_assignment`: dismiss a blocker, advance a page with no
controls, act on the first unattempted field, walk a gate's remaining side.

Gate rule, by shape: `toggle` with no options is a two-sided gate whose sides
are `"true"` / `"false"`; anything in `{toggle, select}` with exactly two
options is a gate on those two labels; three or more options is not a gate and
is walked once. `ControlType` is `text|select|toggle|date|number|other`, so the
spec's `switch`, `boolean`, `checkbox` and `radio` names have no separate case
here -- `toggle` and `select` are the whole of the rule.

Intent from control shape: `text|number|date` -> `fill`; a control with options
-> `select`; `toggle` with no options -> `check`; `select`/`other` with
`options: None` -> `expand`.

A gate's *first* side is named by Frontier, not left to the filler. The board
cannot know which side is still owed if it did not choose the first one.

Loop's `run_crawl` now runs perceive -> observe -> assign -> fill -> perceive
until the board is done. `fill()` imports `trailblazer.agents.form_filler.
form_filler.fill_one` lazily; that module is an empty file, so the import raises
`ImportError`, `fill()` returns `None`, and the walk logs the assignment and
stops instead of re-perceiving an unchanged page. A page still producing
assignments after `MAX_ASSIGNMENTS = 60` raises `RuntimeError`.

Every `observe`, `assign` and `done` is recorded against `RunLedger` with
`usd=0.0`. Frontier makes no model call, so no step is ever `unpriced`.

## Files touched

- `src/trailblazer/agents/frontier/board.py` (new)
- `src/trailblazer/agents/frontier/frontier.py` (new)
- `src/trailblazer/agents/frontier/__init__.py` (re-exports)
- `src/trailblazer/loop/orchestrator.py` (walk loop, ledger, Frontier wiring)
- `tests/test_frontier.py` (new, 32 tests)
- `tests/test_loop.py` (new, 3 tests)

Nothing under `contracts/`, `agents/scraper/`, `agents/form_filler/`,
`agents/generator/` or `agents/validator/` was modified.

## Test results

`uv run pytest tests/ -q`, with `CARRIER_URL` set in the environment:

```
........................................................................ [ 82%]
...............                                                          [100%]
87 passed in 10.09s
```

87 = 52 pre-existing + 35 new. The new tests alone:

```
...................................                                      [100%]
35 passed in 0.38s
```

Without `CARRIER_URL` set, two pre-existing tests fail:

```
FAILED tests/test_api.py::test_crawl_returns_the_scraper_result - assert 400 ...
FAILED tests/test_api.py::test_a_crawl_failure_is_a_500_with_the_cause_in_detail
2 failed, 85 passed in 10.04s
```

Cause: `.env` is gitignored and does not exist in this worktree, so
`Settings.carrier_url` is `None`, `resolve_carrier_creds` raises, and
`/v0/carriers/{id}/crawl` returns 400 before reaching the patched `run_crawl`.
Both tests pass in the main checkout, which has `.env`. Unrelated to this
change; no source file was edited to make them pass.

## Not done, and why

**Backtracking is not implemented.** Walking a gate's second side requires the
page returned to its pre-choice state, which means renavigation plus a replay of
the action prefix. That prefix is the Generator's output and the Generator is
not built. The second-side assignment is issued against the page as it stands,
the fieldId is appended to `Board.issued_without_reset`, and a warning naming
the fieldId is logged. `summary()["issuedWithoutReset"]` carries the list so the
completion assertion can see which branches were walked from a dirty page.

**`openSet` is ignored.** The spec's gate rule excludes `openSet: true` from
being a gate. `Control` has no `openSet` field and no other field distinguishing
a sampled option list from a closed one, so a two-option control that is really
an open set is treated as a gate here. The plan permits ignoring this clause
when the information is unavailable.

## Contract limitations hit

`Assignment.value` for a checkbox-shaped gate carries the literal strings
`"true"` / `"false"`. `Assignment` has no field for "which side of a boolean",
and `value` is documented as "Text for `fill`, option label for `select`". The
filler must read `intent == "check"` together with `value` to know which state
to leave the control in. No contract was edited.

`PageDescription.blockers` is `list[str]` with no locator, so the element that
would dismiss a blocker cannot be addressed from the blocker itself. Frontier
finds it by matching the page's `actions` labels against a fixed word list
(`accept`, `agree`, `dismiss`, `close`, `got it`, `ok`, `continue`, `allow`). A
blocker whose dismiss control carries none of those words is logged as
unclearable and the walk proceeds past it.

`Control.fieldId` is a per-page counter, so a board is retired whenever
`stageId` changes rather than being carried across pages.
