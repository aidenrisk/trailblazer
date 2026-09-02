"""Run the real pipeline against the live carrier portal and watch Frontier.

    BrowserSession -> perceive (real scraper, real LLM) -> Frontier -> StubFormFiller
                           ^                                              |
                           +----------------- Loop --------------------- --+

This is an integration smoke test, not a unit test. It:
  - launches a real Chromium and navigates to CARRIER_URL
  - runs the real scraper (which calls the LLM) on every look
  - drives the real Frontier over the descriptions it returns
  - uses the STUB FormFiller, which does not touch the page

That last point bounds what this can prove. Because nothing actually types
into the form, every look returns a near-identical page, so we exercise the
contract wiring (does a real PageDescription flow through Frontier, are the
option locators usable, does revealedBy arrive) but not the page's reaction to
input. The real FormFiller lands on feat/formfiller.

COSTS TOKENS: one LLM call per look. --max-steps caps it.

    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/live_pipeline.py
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/live_pipeline.py --max-steps 6
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/live_pipeline.py --url https://...
"""

import argparse
import datetime
import json
import logging
import pathlib
import sys
import time

from pydantic import BaseModel

from playwright.sync_api import sync_playwright

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.form_filler.stub import StubFormFiller
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.contracts import PerceiveRequest, ScraperResult
from trailblazer.loop.orchestrator import Loop
from trailblazer.shared.config import get_settings

log = logging.getLogger("live")
STEP = 0


def setup_logging(name: str) -> pathlib.Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = pathlib.Path("logs")
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"{name}-{stamp}.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-28s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(path, encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    return path


def dump(name: str, obj) -> None:
    global STEP
    STEP += 1
    if isinstance(obj, BaseModel):
        payload = obj.model_dump()
    elif isinstance(obj, list) and obj and isinstance(obj[0], BaseModel):
        payload = [o.model_dump() for o in obj]
    else:
        payload = obj
    banner = f"[{STEP:02d}] {name}"
    log.info("")
    log.info("--- %s %s", banner, "-" * max(0, 60 - len(banner)))
    for line in json.dumps(payload, indent=2, default=str).splitlines():
        log.info("    %s", line)


class StepCap(Exception):
    """Raised to stop the walk once --max-steps looks have been spent."""


def board_table(frontier) -> list[str]:
    """The board as one line per control."""
    rows = []
    for c in frontier.board.controls:
        if c.options is None:
            state = "options unknown"
        elif not c.options:
            state = "plain field"
        else:
            state = "walked=[%s] pending=[%s]" % (
                ",".join(o.label for o in c.walked),
                ",".join(o.label for o in c.pending),
            )
        reveal = f"  <- revealed by {c.revealedBy.fieldId}={c.revealedBy.equals}" if c.revealedBy else ""
        rows.append(
            "    %s %-8s %-28s %s%s"
            % ("[x]" if c.explored else "[ ]", c.fieldId, c.label[:28], state, reveal)
        )
    return rows


class TracedFrontier(FrontierAgent):
    """FrontierAgent that narrates every decision.

    Logs, per call: what feedback it absorbed, the board before and after, which
    control it selected and why, and the assignment it emitted.
    """

    def on_page(self, job, page, scrape=None, fill_report=None):
        n = getattr(self, "_calls", 0) + 1
        self._calls = n

        log.info("")
        log.info("#" * 78)
        log.info("# FRONTIER CALL %d", n)
        log.info("#" * 78)

        if scrape is not None:
            log.info("  in  <- scraper: polarity=%s added=%s removed=%s changed=%s",
                     scrape.polarity, scrape.addedControls,
                     scrape.removedControls, scrape.changedControls)
        if fill_report is not None:
            log.info("  in  <- filler : field=%s ok=%s discovered=%s chose=%s",
                     fill_report.fieldId, fill_report.ok,
                     None if fill_report.discoveredOptions is None
                     else [o.label for o in fill_report.discoveredOptions],
                     fill_report.chosenOption)
        if scrape is None and fill_report is None:
            log.info("  in  <- first call, no feedback yet")

        before = board_table(self)
        outcome = super().on_page(job, page, scrape, fill_report)
        after = board_table(self)

        if before != after:
            log.info("  board (changed):")
            for row in after:
                log.info("%s", row)
        else:
            log.info("  board unchanged (%d controls)", len(self.board.controls))

        if hasattr(outcome, "paths"):
            log.info("  out -> WALK: %d replayable paths, status=%s",
                     len(outcome.paths), self.board.status)
        else:
            detail = {
                k: v for k, v in outcome.model_dump().items() if k != "type"
            }
            log.info("  out -> %s %s   (status=%s)",
                     outcome.type, detail or "", self.board.status)
        return outcome


def _drive(page, args, settings, frontier, looks, log_path) -> int:
    """Run the pipeline over an already-open page. Shared by both entry paths."""

    def perceiver(request: PerceiveRequest) -> ScraperResult:
        looks["n"] += 1
        if looks["n"] > args.max_steps:
            raise StepCap(f"hit --max-steps={args.max_steps}")
        log.info("")
        log.info(
            ">>> LOOK %d  page_index=%s  assignment=%s",
            looks["n"],
            request.page_index,
            request.assignment,
        )
        result = perceive(page, request, settings)
        log.info(
            "    scraper: stage=%s controls=%d polarity=%s blockers=%d",
            result.page.stageId,
            len(result.page.controls),
            result.polarity,
            len(result.page.blockers),
        )
        return result

    first = perceiver(
        PerceiveRequest(
            job_id="live",
            page_index=1,
            objective="Describe this form page so every control can be filled.",
        )
    )

    log.info("")
    log.info("=" * 78)
    log.info("  WHAT THE SCRAPER SAW (%d controls)", len(first.page.controls))
    log.info("=" * 78)
    for c in first.page.controls:
        log.info(
            "  %-7s %-32s %-7s req=%-5s %s",
            c.fieldId,
            c.label[:32],
            c.type,
            c.required,
            c.locator[:44],
        )
        if c.options is not None:
            for o in c.options:
                log.info(
                    "           option %-24s %s",
                    o.label[:24],
                    o.locator or "(no node - select_option on parent)",
                )
    log.info("  next=%s", first.page.next)
    log.info("  back=%s", first.page.back)
    if first.page.blockers:
        for b in first.page.blockers:
            log.warning("  BLOCKER: %s", b)
    dump("first ScraperResult", first)

    walk = None
    loop = Loop(perceiver, frontier, StubFormFiller(), recursion_limit=200)
    try:
        walk = loop.fill_form("live", first.page)
    except StepCap as e:
        log.warning("")
        log.warning("STOPPED: %s", e)
    except Exception:
        log.exception("pipeline raised")

    # ---- what Frontier did ------------------------------------------------
    log.info("")
    log.info("=" * 78)
    log.info(
        "  FINAL BOARD  status=%s  stage=%s",
        frontier.board.status,
        frontier.board.currentStageId,
    )
    log.info("=" * 78)
    for row in board_table(frontier):
        log.info("%s", row)

    log.info("")
    log.info("  OBSERVED ACTIONS (%d) - in-place order", len(frontier.walk_log))
    for i, s in enumerate(frontier.walk_log, 1):
        log.info(
            "   %2d. %-7s %-8s %-40s %s",
            i,
            s.action,
            s.fieldId or "-",
            (s.locator or "(no node)")[:40],
            s.option or s.value or "",
        )

    if walk is not None:
        log.info("")
        log.info("  %d REPLAYABLE PATHS", len(walk.paths))
        for n, path in enumerate(walk.paths, 1):
            pins = ", ".join(f"{f}={o}" for f, o in path.choices.items()) or "(no branches)"
            log.info("")
            log.info("   PATH %d/%d  %s", n, len(walk.paths), pins)
            for i, s in enumerate(path.steps, 1):
                log.info(
                    "      %2d. %-7s %-8s %-38s %s",
                    i,
                    s.action,
                    s.fieldId or "-",
                    (s.locator or "(no node)")[:38],
                    s.option or s.value or "",
                )

    unexplored = [c.fieldId for c in frontier.board.controls if not c.explored]
    log.info("")
    log.info("  unexplored : %s", unexplored or "none")
    log.info("  looks spent: %d", looks["n"])
    log.info("  trace      : %s", log_path)
    log.info("=" * 78)
    return 0


def _run_attached(args, settings, frontier, looks, log_path) -> int:
    """Attach to a Chrome already serving CDP and scrape the tab it has open.

    Deliberately does not navigate: the point is to use a session someone has
    already logged into, on whatever page they left it.
    """
    endpoint = f"http://127.0.0.1:{settings.cdp_port}"
    log.info("attaching to %s (no navigation)", endpoint)

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(endpoint)
        if not browser.contexts:
            log.error("attached but the browser has no contexts")
            return 4
        context = browser.contexts[0]
        pages = [p for p in context.pages if not p.url.startswith("devtools://")]
        if not pages:
            log.error("attached but found no open page")
            return 4

        page = pages[0]
        if args.wait_for and args.wait_for not in page.url:
            match = next((p for p in pages if args.wait_for in p.url), None)
            if match is None:
                log.error(
                    "no open tab matches %r. tabs: %s",
                    args.wait_for,
                    [p.url[:70] for p in pages],
                )
                return 3
            page = match

        log.info("using tab: %s", page.url)
        if len(pages) > 1:
            log.info("  (%d tabs open; others ignored)", len(pages))

        return _drive(page, args, settings, frontier, looks, log_path)
    finally:
        # Leave the browser alone -- it is the user's session, not ours.
        pw.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=None, help="override CARRIER_URL")
    ap.add_argument("--max-steps", type=int, default=12, help="max looks (each = 1 LLM call)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument(
        "--wait-for",
        default=None,
        metavar="SUBSTRING",
        help="hold before perceiving until the page URL contains SUBSTRING. "
        "Use this to log in by hand in the launched window and navigate to the "
        "page you want scraped. Implies --headed.",
    )
    ap.add_argument(
        "--wait-timeout", type=int, default=300, help="seconds to wait for --wait-for"
    )
    ap.add_argument(
        "--attach",
        action="store_true",
        help="attach to a Chrome already listening on CDP_PORT and scrape the tab "
        "that is open, instead of launching a fresh browser. Use this to keep an "
        "existing logged-in session. Does not navigate anywhere.",
    )
    args = ap.parse_args()
    if args.wait_for:
        args.headed = True

    log_path = setup_logging("live")
    settings = get_settings()
    url = args.url or settings.carrier_url
    if not url:
        log.error("no URL: set CARRIER_URL in .env or pass --url")
        return 2

    log.info("=" * 78)
    log.info("  LIVE PIPELINE")
    log.info("  url        = %s", url)
    log.info("  model      = %s via %s", settings.openrouter_model, settings.llm_provider)
    log.info("  perceiver  = %s", settings.scraper_perceiver)
    log.info("  max looks  = %d  (each look is one LLM call)", args.max_steps)
    log.info("  formfiller = StubFormFiller (does NOT touch the page)")
    log.info("=" * 78)

    frontier = TracedFrontier()
    looks = {"n": 0}

    if args.attach:
        return _run_attached(args, settings, frontier, looks, log_path)

    with BrowserSession(cdp_port=settings.cdp_port, headed=args.headed or settings.headed) as session:
        page = session.goto(url)
        log.info("navigated: %s", page.url)

        if args.wait_for:
            # The browser this session launched is a fresh profile, so it is
            # logged out. Rather than automate a login that does not exist yet,
            # hand the window to a person and wait for them to arrive somewhere.
            log.info("")
            log.info("=" * 78)
            log.info("  WAITING FOR YOU")
            log.info("  A Chromium window is open. Log in there, then navigate to")
            log.info("  the page you want scraped.")
            log.info("  Watching for a URL containing: %r", args.wait_for)
            log.info("  Giving up after %ds. Nothing is scraped until then, so", args.wait_timeout)
            log.info("  no LLM calls are spent while waiting.")
            log.info("=" * 78)

            deadline = time.time() + args.wait_timeout
            last = None
            while time.time() < deadline:
                try:
                    current = page.url
                except Exception:  # navigation in flight
                    time.sleep(1)
                    continue
                if current != last:
                    log.info("  at: %s", current)
                    last = current
                if args.wait_for in current:
                    log.info("  MATCHED - starting the pipeline")
                    break
                time.sleep(2)
            else:
                log.error("timed out waiting for %r (last url: %s)", args.wait_for, last)
                return 3

            page.wait_for_load_state("networkidle", timeout=15000)

        return _drive(page, args, settings, frontier, looks, log_path)


if __name__ == "__main__":
    sys.exit(main())
