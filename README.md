# trailblazer

Crawls a carrier's dynamic SPA application form and produces a replay script plus metadata.

## Install

```bash
uv sync
uv run playwright install chromium
```

`playwright install` downloads Chromium to `~/Library/Caches/ms-playwright`, outside the
venv. It is a per-user download, not a system-wide install, and `uv` cannot manage it
because `uv` handles Python distributions only.

Then copy `.env.example` to `.env` and fill in a key:

```bash
cp .env.example .env
```

`LLM_PROVIDER=openrouter` needs `OPENROUTER_API_KEY`; `anthropic` needs an
`ANTHROPIC_API_KEY` from the Anthropic Console. A Claude Code OAuth token
(`sk-ant-oat01-*`) will not work — the Messages API rejects it.

### Carriers and credentials

A crawl starts from a carrier's own portal URL, looked up by `carrier_id` (its slug or
numeric id) from the `carriers` and `carrier_creds` tables. Start the project database
and register a carrier:

```bash
docker compose up -d db
uv run python scripts/upsert_carrier_creds.py --slug pie --name "Pie Insurance" \
  --login-url https://partner.pieinsurance.com/login --username agent@example.com
```

The script prompts for the password and stores it encrypted (AES-256-GCM) under
`CRED_ENCRYPTION_KEY`; generate one with `openssl rand -hex 32`. Leave the key empty in
development and passwords are stored plaintext, with a warning. Pass `--mfa` (and
`--mfa-domain <sender domain>`) for a portal that challenges with an emailed code, and
`--no-password` for one that signs in with a code only.

## Run

```bash
uv run python -m http.server 8765 --directory tests/fixtures &
uv run trailblazer --url http://localhost:8765/form.html --headed
```

Prints a `ScraperResult` as JSON. `--out DIR` also writes
`DIR/<job_id>/page_description.json`.

If the launch reports that the CDP port is in use, a Chrome instance is likely already
holding 9222. Set `CDP_PORT` to a free port.

### HTTP API

```bash
uv run trailblazer serve --port 8000
# or: uv run uvicorn trailblazer.api:app --port 8000
```

`POST /v0/carriers/{carrier_id}/crawl` runs one crawl synchronously and returns the
`ScraperResult`. The request blocks for the length of the crawl; there is no job queue.

```bash
curl -X POST http://127.0.0.1:8000/v0/carriers/pie/crawl \
  -H 'content-type: application/json' \
  -d '{"insuranceTypes":["workers_comp"],"businessTypes":["contractors"],"headed":false}'
```

The client never supplies a URL or credentials: both come from the carrier's row (see
*Carriers and credentials* above). To crawl the local fixture, register a carrier whose
`--login-url` is `http://localhost:8765/form.html`.

400 when `carrier_id` has no credentials on file, 503 when the database is unreachable,
422 on a malformed body, 500 when the crawl itself fails, with the cause in `detail`.

### The full chain, offline

The Loop that drives all five agents runs today against stub agents, with no browser and
no model, and traces every contract object as it crosses an agent boundary:

```bash
uv run python scripts/demo_frontier_walk.py basic
```

### Logging and cost

`LOG_LEVEL` (default `INFO`) sets the level for the `trailblazer` logger; both the CLI and
the server honour it. INFO covers browser launch, navigation, perceive start/end with a
duration, one line per LLM call with its USD cost, the diff polarity, and where output was
written. DEBUG adds the extractor payload size and each locator that failed uniqueness.

Cost appears in the logs only — it is not in `ScraperResult` and is not written to disk.
OpenRouter reports the amount it actually charged; the Anthropic path is priced from a small
local table, and a model missing from it logs a warning and reports `usd=unknown` rather
than guessing.

## Test

```bash
uv run pytest tests/ -v
```

No API key and no database required: the tests cover extraction, the contract validators,
the diff, `finalize()`, the locator restoration that overrules the model, cost parsing from
constructed provider responses, the endpoint with the crawl patched out, the Frontier walk
end to end through the Loop against stub agents, credential resolution against an injected
row, encryption against a vector produced by Roadrunner's `crypto.js`, the MFA toolkit
against a scripted inbox, and the chain logging into a stand-in portal with the Scraper's
model replaced by a payload echo and a test-only login executor in the FormFiller's slot
(that agent is not built yet). The live LLM call is the only thing not exercised. The per-carrier login lock tests run when the project Postgres is up and skip
otherwise. The repository carries no synthetic web pages: the stand-in portal pages live as
strings in `tests/pages.py` and are written to a temp directory at run time.

Two opt-in tests hit real systems and skip by default:

```bash
# the shared backend inbox, read-only (never consumes a code); a 204 is a pass
TRAILBLAZER_LIVE=1 TRAILBLAZER_LIVE_CARRIER=thimble uv run pytest tests/live/test_inbox_live.py -v
# log into a real carrier registered in the database; costs one code on some portals
TRAILBLAZER_LIVE_LOGIN=1 TRAILBLAZER_LIVE_CARRIER=thimble HEADED=true uv run pytest tests/live/test_login_live.py -v -s
```
