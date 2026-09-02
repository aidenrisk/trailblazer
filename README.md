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
  -d '{"insuranceTypes":["workers_comp"],"businessTypes":["contractors"],"headed":false,
       "url":"http://localhost:8765/form.html"}'
```

`url` is a temporary field. A crawl starts from a carrier's portal URL, which belongs in a
carriers table that does not exist yet — supply it per-request, or set `CARRIER_URL`. Both
go away once `carrier_id` can be looked up.

400 when no URL is available, 422 on a malformed body, 500 when the crawl itself fails,
with the cause in `detail`.

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

No API key required: the tests cover extraction, the contract validators, the diff,
`finalize()`, the locator restoration that overrules the model, cost parsing from
constructed provider responses, and the endpoint with the crawl patched out. The live LLM
call is the only thing not exercised.
