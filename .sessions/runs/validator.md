# Validator run report

Branch `feat/validator`, cut from `feat/scraper` (`8c7b2e4`).

## What was built

`validate(request, settings, config_path=None, ledger=None, timeout_s=900)`
runs `node <script> <answers.json> [--config <creds.json>]` with `cwd` set to
the script's directory and `HEADLESS` derived from `request.headed` or
`settings.headed`, then reports a `ValidationResult`.

Outcome reading follows section 6's order and names the channel that answered:
`last-run.json` beside the script, then the last `RRSTATUS <json>` line on
stdout, then a scrape of an outcome word out of the combined logs. The source
is logged on every run (`source=` in the `replay end` line).

The outcome vocabulary is closed. `appetite_decline` and `appetite decline`
normalize to `appetite-decline`; anything unrecognized becomes `quote` when
`reachedQuote` is set and `stuck` otherwise. `exitCode` is 0 for `quote` and
`appetite-decline`, 1 otherwise. A decline is reported as a successful run
(`ValidationResult.success` is True) and is recorded in the ledger with
`ok=True`.

A script that produces no status on any channel is `stuck`. `stoppedReason` is
taken from Node's `<ErrorName>: <message>` line, matched directly rather than
by taking the last stderr line, because Node prints a stack and a
`Node.js vX.Y.Z` banner after it.

`static_checks(script_path) -> list[str]` implements the seven persist-time
rules as regexes over the script source with JS comments stripped first. They
are warn-only: every warning is logged and recorded in the ledger, and the run
proceeds regardless.

Two ledger steps are recorded per call, both `agent="validator"`:
`static_checks` (`ok` false when any warning fired) and `validate` (`ok`
follows `result.success`). `usd` is 0.0 on both — the Validator makes no LLM
calls, so `unpriced` never applies.

Two defensive behaviours not spelled out in the plan:

- `last-run.json` is deleted before the run. It is the preferred source and
  outlives the process that wrote it, so without this a script that crashes
  before writing reports the previous run's quote.
- `answers_path` and `config_path` are resolved to absolute paths, because the
  child process runs with `cwd` set to the script's directory.

## Files touched

Added:

- `src/trailblazer/agents/validator/static_checks.py`
- `tests/test_validator.py`
- `tests/fixtures/scripts/` — `quote.js`, `file_only.js`, `stdout_only.js`,
  `file_beats_stdout.js`, `decline_underscore.js`, `crash.js`,
  `unknown_outcome.js`, `unknown_outcome_no_quote.js`, `answers.json`
- `tests/fixtures/scripts/bad/` — `bad_manifest_names.js`,
  `no_status_output.js`, `human_behavior_frame.js`, `bare_login_lock.js`,
  `hardcoded_gate.js`, `required_fallback.js`, `wrong_cred_keys.js`

Modified:

- `src/trailblazer/agents/validator/validator.py` (was an empty stub)
- `src/trailblazer/agents/validator/__init__.py` (was empty)

Nothing under `contracts/`, `agents/scraper/`, `agents/frontier/`,
`agents/form_filler/`, `agents/replay_gen/` or `.sessions/` was modified.

## Test results

42 new tests in `tests/test_validator.py`. They are skipped as a module when
`node` is not on PATH. No test drives a browser or a carrier portal; every
replay is a hand-written fixture, since the Generator does not exist yet.

`uv run pytest tests/ -q`:

```
tests/test_api.py:151: AssertionError
=========================== short test summary info ============================
FAILED tests/test_api.py::test_crawl_returns_the_scraper_result - assert 400 ...
FAILED tests/test_api.py::test_a_crawl_failure_is_a_500_with_the_cause_in_detail
2 failed, 92 passed in 14.43s
```

The two failures are pre-existing and environment-dependent, not caused by this
branch. Both assert on `POST /v0/carriers/pie/crawl` and get 400 because
`resolve_carrier_creds("pie")` raises when `CARRIER_URL` is unset; the tests
stub `trailblazer.api.run_crawl` but not the creds lookup, which runs first and
returns 400 before `run_crawl` is reached:

```
RuntimeError: no carrier URL configured: set CARRIER_URL in .env. (dev stub -- carrier_creds.login_url is not wired up yet)
```

`.env` is gitignored, so a fresh worktree has none. Supplying the value makes
the whole suite pass — 52 pre-existing plus 42 new:

`CARRIER_URL=https://example.com uv run pytest tests/ -q`:

```
........................................................................ [ 76%]
......................                                                   [100%]
94 passed in 13.48s
```

The same two tests fail on the untouched `feat/scraper` tip in this worktree,
before any Validator code existed: `2 failed, 50 passed in 14.83s`.

## Deviations from the plan

The plan's `static_checks(script_path)` and `validate(request, settings)`
signatures are implemented as specified. `validate` takes three additional
keyword arguments with defaults — `config_path`, `ledger`, `timeout_s` — so the
two-argument call in the plan still works.

The plan listed "every recorded stage referenced by the script" among section
6's script rules. It is not implemented: it needs the recorded stage list,
which lives in `GenerationState.stages` and is produced by the Generator. The
Validator's input contract, `ValidationRequest`, carries only `job_id`,
`script_path`, `answers_path` and `headed`, so the stage list cannot reach it
without a contract change. Not made, per instruction.

## Contract observations

`ValidationResult` has no field for the static-check warnings. They are logged
and recorded in the ledger, but a caller receiving only the `ValidationResult`
cannot see them. Section 6 makes them warn-only and says the flow stays draft,
so something has to carry the draft/clean distinction to whoever persists the
flow. No contract change was made.

`ValidationResult` also omits `quoteSaved` and `documents`, both present in
section 6's status JSON. Scripts that report them have those values dropped.

`ValidationRequest.answers_path` is documented as `{client, answers}` or a bare
answers object. The Validator passes the file through to the script unread, so
either shape works without it needing to know which.
