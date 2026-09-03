# Generator run report

Branch `feat/generator`, based on `feat/scraper` at `8c7b2e4`.

## What was built

The Generator agent: appends one walked action to the three crawl artifacts in a
single all-or-none step. `append(request, ledger=None) -> GenerationState`.

The three-way write is structural. `append` builds the question, the metadata
field and the script block in local variables, and only after all three exist
does `_commit` attach them to the documents and `_flush` write the files. There
is no code path that reaches one document and not the others. `_assert_stages_agree`
runs at every page boundary and compares the metadata stage list against the
stage markers regex-scraped out of the assembled script, plus the stage names the
questions reference; a mismatch raises `ArtifactMismatch`.

`questionId` allocation is owned here. `q_001` onward, monotonic across the whole
flow, keyed on `(stageId, fieldId)` because `Control.fieldId` is a per-page
counter reset at every perceive. A second capture of an already-allocated key
reconciles into the existing entry — requiredness ORs, the richer option list
wins, a later `exampleValue` replaces the earlier one — rather than appending a
duplicate.

`canonical` resolves against `agents/generator/canonicals.json`, seeded with the
twelve names in the arch doc's examples. Matching is substring-on-normalized-label,
longest phrase first so `legal business name` does not collapse into `legal name`.
Minted names are recorded in `Generator.minted_canonicals` as `canonical_aliases`
candidates rather than entering the vocabulary silently.

Type mapping is `reconcile.catalog_type`, transcribed from arch doc 3.5 including
the four post-rules. `options` is emitted only when the final type is `enum`.

Guards that raise rather than write: a credential literal on a question whose
canonical matches `password|secret|token|otp|api_key` (`CredentialLeak`), and a
selector embedding a credential. `$EMAIL` / `$PASSWORD` / `$OTP` pass.

The replay script emitter references its pair as the literal strings
`onboarding-<slug>.metadata.json` / `onboarding-<slug>.questions.json`, writes
`last-run.json` and prints one `RRSTATUS <json>` line, carries the pay/bind
denylist inline, reads every answer by canonical through `requiredAnswer` /
`optionalAnswer`, and exits 0 for `quote` and `appetite-decline`.

## Files touched

Added, all under `src/trailblazer/agents/generator/`:

- `__init__.py` — exports `Generator`, `CredentialLeak`, `ArtifactMismatch`
- `generator.py` — the agent, id allocation, the three-way write, the guards
- `artifacts.py` — the questions and metadata document shapes (3.3, 3.4)
- `script.py` — the replay script emitter (section 6)
- `reconcile.py` — merge-field stripping, option cleaning, capture merging, 3.5 mapping
- `canonical.py` — vocabulary lookup and minting
- `canonicals.json` — the seed vocabulary

Added: `tests/test_generator.py` (49 tests).

Nothing outside `agents/generator/` and `tests/` was modified. `contracts/`,
the other agents' directories and `.sessions/03-architecture-and-spec.md` were
read only.

## Test results

Full suite, with `CARRIER_URL` set:

```
$ CARRIER_URL=https://example.com uv run pytest tests/ -q
........................................................................ [ 71%]
.............................                                            [100%]
101 passed in 11.24s
```

52 pre-existing + 49 new = 101.

Generator tests alone:

```
$ uv run pytest tests/test_generator.py -q
.................................................                        [100%]
49 passed in 0.26s
```

Without `CARRIER_URL`, two pre-existing API tests fail:

```
$ uv run pytest tests/ -q
tests/test_api.py:151: AssertionError
=========================== short test summary info ============================
FAILED tests/test_api.py::test_crawl_returns_the_scraper_result - assert 400 ...
FAILED tests/test_api.py::test_a_crawl_failure_is_a_500_with_the_cause_in_detail
2 failed, 99 passed in 10.50s
```

These two fail on the base commit `8c7b2e4` before any Generator code existed.
Cause: neither test patches `resolve_carrier_creds`, so with no `.env` in the
worktree credential resolution raises and the endpoint returns 400 before
reaching the code under test. `.env` is gitignored and absent from the worktree
but present in the main checkout, which is why the plan states 52. Not a
Generator regression and not fixed here — `tests/test_api.py` is outside scope.

The emitted script is checked with `node --check` in
`test_the_generated_script_is_valid_javascript`, skipped when node is absent.
Node 24 is present here and the check passes.

## Not done, and why

**No `contracts/artifacts.py`.** Arch doc section 7 places the questions and
metadata shapes at `contracts/artifacts.py` and marks them NOT WRITTEN. The task
forbids modifying `contracts/`, so they are in
`agents/generator/artifacts.py`. The module is schema only, with no generator
logic, so relocating it is a file move plus an import change.

**No login stage.** Arch doc 9 records login/MFA as not built, and no agent on
this branch produces a login `FillReport`. The metadata carries `loginUrl` and
the credential guard enforces placeholders, but no login stage is emitted and no
script block fills credentials. An emitter for it was written and then removed as
an uncalled helper.

**No `satisfies`, `derivesFrom`, `description` population.** The fields exist on
`Question` per 3.3 but nothing in `GenerationRequest` or `FillReport` carries the
cross-question relationships they express. Left `None` rather than guessed;
omitting `satisfies` makes the chat repeat itself, so this needs an input that
does not exist yet.

**No `eligibilityRules`, `branchExploration`, `documents`, `config` population.**
Present in the metadata shape and emitted as empty. A declining answer, gate
coverage and the re-entry budget are Frontier's board, which is not on this
branch; `GenerationRequest` carries no field for any of them.

**No completion assertion.** Arch doc 4 assigns it to Loop, which grades the
artifacts the Generator wrote. `GenerationState` returns the inputs it needs
(`questionIds`, `stages`, `scriptSteps`, `unresolvedCanonicals`).

## Contract and spec ambiguity

**`Assignment.Intent` disagrees between 3.2 and the written contract.** Arch doc
3.2 lists `fill | select | check | expand-and-select`; `contracts/assignment.py`
defines `fill | select | check | expand | advance`. The written contract was
followed, since it is the code the filler will emit against. `advance` has no
counterpart in 3.2 and is treated as a stage exit that writes no question.

**`FillReport.action` in 3.2 does not exist.** 3.2 says the report carries
`action` "what was actually done, for the replay script". The written contract
has `intent` and `locator` instead. The script block is derived from `intent`
plus the metadata field's shape.

**`GenerationState.stages` is `list[str]` but 3.4 stages are objects.** The state
returns stage names only. Loop's completion assertion compares counts and names,
which the list supports; the full stage objects are on `Generator.metadata_doc`.

**Two-sided gate `selectorYes` / `selectorNo` label matching.** 3.4 gives the
shape but not how a non-Yes/No two-option gate maps onto it. Implemented as:
match `yes|true` and `no|false` case-insensitively, else fall back to the first
and second option in order. A two-option gate labelled e.g. `Owned` / `Leased`
therefore gets `selectorYes=Owned`. Flagging this as a guess — the alternative is
to emit the generic per-option shape for anything not literally Yes/No, which
would be safer but leaves 3.4's gate shape unused for most gates.

**`unit` when no rejection revealed one.** 3.3 requires `unit` and 3.5's mapping
keys off it, but `unit` is only discoverable from a `FillReport.constraint`,
which exists only after a rejection. Unrejected fields would carry no unit and
map by HTML type alone, so a dollar amount in a text box would type as `string`
rather than `currency`. `_infer_unit` guesses from label keywords as a fallback.
This is a heuristic, not measured, and it is the weakest part of the mapping.

**`CatalogType` unions two vocabularies.** 3.3's `type` list (`text|email|tel|…`)
and 3.5's mapped output (`string|enum|boolean|currency|phone|number|date`) are
different vocabularies, and 3.3 does not say which one the artifact's `type`
field holds. The mapped value is written, since 3.5 calls it the catalog type and
`question_catalog` reads it directly. `CatalogType` accepts both so a portal type
passing through unmapped still validates.
