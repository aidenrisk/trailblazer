"""Emit every contract object that crosses an agent boundary. Payloads only.

No narration. Each record is a timestamp, a sequence number, the edge it
crossed, the type, and the payload as JSON:

    2026-09-03T01:45:12.345678  #003  Frontier->FormFiller  FillFieldAssignment
    {
      "type": "fill_field",
      ...
    }

Written to two files:
    <out>.log    the above, human-readable
    <out>.jsonl  one JSON object per record: {ts, seq, edge, type, payload}

Usage:
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/contract_trace.py --attach
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/contract_trace.py --fixture
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/contract_trace.py --attach -o logs/pie

COSTS TOKENS: one LLM call per look. --max-looks caps it.
"""

import argparse
import datetime
import json
import logging
import pathlib
import sys

from playwright.sync_api import sync_playwright
from pydantic import BaseModel

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.form_filler.stub import StubFormFiller
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.contracts import PerceiveRequest, ScraperResult, Walk
from trailblazer.loop.orchestrator import Loop, assignment_values
from trailblazer.shared.config import get_settings

FIXTURE = pathlib.Path("tests/fixtures/form.html")


class Trace:
    """Writes one record per contract crossing, to stdout and two files."""

    def __init__(self, out: pathlib.Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        self.text = out.with_suffix(".log").open("w", encoding="utf-8")
        self.jsonl = out.with_suffix(".jsonl").open("w", encoding="utf-8")
        self.seq = 0

    def record(self, edge: str, obj) -> None:
        self.seq += 1
        ts = datetime.datetime.now().isoformat(timespec="microseconds")

        if isinstance(obj, BaseModel):
            type_name = type(obj).__name__
            payload = obj.model_dump(mode="json")
        elif isinstance(obj, list) and obj and isinstance(obj[0], BaseModel):
            type_name = f"list[{type(obj[0]).__name__}]"
            payload = [o.model_dump(mode="json") for o in obj]
        else:
            type_name = type(obj).__name__
            payload = obj

        header = f"{ts}  #{self.seq:03d}  {edge}  {type_name}"
        body = json.dumps(payload, indent=2, default=str)

        for stream in (sys.stdout, self.text):
            stream.write(f"\n{header}\n{body}\n")
            stream.flush()

        self.jsonl.write(
            json.dumps(
                {"ts": ts, "seq": self.seq, "edge": edge, "type": type_name, "payload": payload},
                default=str,
            )
            + "\n"
        )
        self.jsonl.flush()

    def close(self) -> None:
        self.text.close()
        self.jsonl.close()


class Cap(Exception):
    pass


class TracedFrontier(FrontierAgent):
    """Records the board after every decision. The board is a contract too."""

    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self._trace = trace

    def on_page(self, job, page, scrape=None, fill_report=None):
        # The exact PageDescription Frontier receives. This is the merged one:
        # a radio group arrives as ONE control carrying both options, not one
        # control per button -- when the scraper merges it.
        self._trace.record("Loop->Frontier", page)
        outcome = super().on_page(job, page, scrape, fill_report)
        self._trace.record("Frontier:board", self.board)
        edge = "Frontier->ReplayGen" if isinstance(outcome, Walk) else "Frontier->FormFiller"
        self._trace.record(edge, outcome)
        return outcome


class TracedFiller(StubFormFiller):
    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self._trace = trace

    def execute(self, job, stage_id, assignment):
        report = super().execute(job, stage_id, assignment)
        self._trace.record("FormFiller->Loop", report)
        return report


def run(page, args, settings, trace: Trace) -> int:
    looks = {"n": 0}
    frontier = TracedFrontier(trace)

    def perceiver(request: PerceiveRequest) -> ScraperResult:
        looks["n"] += 1
        if looks["n"] > args.max_looks:
            raise Cap(f"max_looks={args.max_looks}")
        trace.record("Loop->Scraper", request)
        result = perceive(page, request, settings)
        trace.record("Scraper->Loop", result)
        return result

    first = perceiver(
        PerceiveRequest(
            job_id="trace",
            page_index=1,
            objective="Describe this form page so every control can be filled.",
        )
    )

    loop = Loop(perceiver, frontier, TracedFiller(trace), recursion_limit=400)
    try:
        walk = loop.fill_form("trace", first.page)
        trace.record("Loop:result", walk)
    except Cap as e:
        trace.record("Loop:halted", {"reason": str(e), "looks": looks["n"]})
    except Exception as e:
        trace.record("Loop:error", {"type": type(e).__name__, "message": str(e)})
        logging.getLogger(__name__).exception("pipeline raised")

    trace.record(
        "Loop:summary",
        {
            "looks": looks["n"],
            "status": frontier.board.status,
            "stageId": frontier.board.currentStageId,
            "controls": len(frontier.board.controls),
            "explored": sum(1 for c in frontier.board.controls if c.explored),
            "unexplored": [c.fieldId for c in frontier.board.controls if not c.explored],
            "actions": len(frontier.walk_log),
        },
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--attach", action="store_true")
    ap.add_argument("--fixture", action="store_true")
    ap.add_argument("--url", default=None)
    ap.add_argument("--max-looks", type=int, default=30)
    ap.add_argument(
        "-o",
        "--out",
        default=None,
        metavar="PREFIX",
        help="write PREFIX.log and PREFIX.jsonl (default logs/contracts-<timestamp>)",
    )
    args = ap.parse_args()

    # Agent logging goes to stderr so it cannot pollute the payload stream.
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s"
    )
    for noisy in ("httpx",):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = get_settings()
    prefix = pathlib.Path(
        args.out
        or f"logs/contracts-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    trace = Trace(prefix)
    trace.record(
        "run:config",
        {
            "model": settings.openrouter_model,
            "provider": settings.llm_provider,
            "perceiver": settings.scraper_perceiver,
            "formfiller": "StubFormFiller",
            "max_looks": args.max_looks,
            "cdp_port": settings.cdp_port,
        },
    )

    try:
        if args.attach:
            pw = sync_playwright().start()
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{settings.cdp_port}")
                pages = [
                    p
                    for ctx in browser.contexts
                    for p in ctx.pages
                    if not p.url.startswith("devtools://")
                ]
                if not pages:
                    trace.record("run:error", {"message": "attached but no page open"})
                    return 4
                trace.record("run:attached", {"url": pages[0].url, "tabs": len(pages)})
                return run(pages[0], args, settings, trace)
            finally:
                pw.stop()

        url = args.url or (FIXTURE.resolve().as_uri() if args.fixture else settings.carrier_url)
        if not url:
            trace.record("run:error", {"message": "no url; use --fixture, --url or --attach"})
            return 2
        with BrowserSession(cdp_port=settings.cdp_port, headed=settings.headed) as session:
            page = session.goto(url)
            trace.record("run:navigated", {"url": page.url})
            return run(page, args, settings, trace)
    finally:
        print(f"\n{prefix}.log\n{prefix}.jsonl", file=sys.stderr)
        trace.close()


if __name__ == "__main__":
    sys.exit(main())
