"""Live end-to-end walk against the tab you already have open. One log file.

    Loop -> Scraper (your browser, real LLM) -> Frontier -> StubFormFiller
                ^                                               |
                +-------------------- Loop ---------------------+

Attaches over CDP to a Chrome you started yourself and drives the page that is
already open -- logged in, past MFA, sitting on the form. It never launches a
browser and never navigates: login is not wired up yet, so the only way to reach
a real form page is to put one there by hand.

Start Chrome once, log in, navigate to the form:

    chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\tb-profile

Then run (off by default -- one LLM call per look):

    $env:TB_LIVE="1"; .venv/Scripts/python.exe -m pytest tests/test_live_walk.py -s

Output is `logs/live-walk-<timestamp>.log`, one line per event, no prose:

    02:41:23.010 INFO  live    scraper>loop   look=1 stage=form_page_1_business_info ...

The FormFiller is still the stub (`form_filler.py` is empty), so nothing types
into the page: this proves the contract wiring against a real PageDescription,
not the form's reaction to input.

Knobs, all optional:
    TB_LIVE=1        required, or the test skips
    TB_LIVE_PORT     CDP port to attach to (default CDP_PORT from .env)
    TB_LIVE_LOOKS    cap on scraper looks (default 25) -- the token budget
    TB_LIVE_MATCH    substring picking the tab, when several are open
"""

import datetime
import json
import logging
import os
import pathlib
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright

from trailblazer.agents.form_filler.stub import StubFormFiller
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.contracts import (
    FillFieldAssignment,
    PerceiveRequest,
    ScraperResult,
    SetOptionAssignment,
    Walk,
)
from trailblazer.loop.orchestrator import Loop
from trailblazer.shared.config import get_settings

JOB = "live"
OBJECTIVE = "Describe this form page so every control can be filled."
LOG_DIR = pathlib.Path("logs")

pytestmark = pytest.mark.skipif(
    os.getenv("TB_LIVE") != "1",
    reason="live test: costs an LLM call per look. Set TB_LIVE=1 to run.",
)


# --------------------------------------------------------------------------- #
# Log formatting. One line per event: `event  k=v k=v`. Values never wrap.
# --------------------------------------------------------------------------- #

log = logging.getLogger("live")


def _s(value: object, limit: int = 60) -> str:
    """One-line, bounded rendering of anything. `-` for absent.

    Newlines are collapsed because a page label containing one would otherwise
    split a record in two and break every `grep` over this file.
    """
    if value is None or value == [] or value == {}:
        return "-"
    if isinstance(value, bool):
        return "y" if value else "n"
    if isinstance(value, (list, tuple)):
        text = ",".join(_s(v, limit) for v in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "~"


def event(name: str, **fields: object) -> None:
    log.info("%-14s %s", name, " ".join(f"{k}={_s(v)}" for k, v in fields.items()))


@pytest.fixture(scope="module")
def log_path():
    """Attach one UTF-8 file handler to the root logger for the whole module.

    Root, not the `trailblazer` logger, so the scraper, the agents and any
    library that logs all land in the same file in real order.
    `configure_logging()` is deliberately not called: it sets
    `propagate = False` on `trailblazer`, which would divert exactly the lines
    this test exists to capture.
    """
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"live-walk-{stamp}.log"

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)-28s %(message)s", "%H:%M:%S"
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("trailblazer").propagate = True
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    yield path

    root.removeHandler(handler)
    handler.close()
    print(f"\nlog: {path}")


# --------------------------------------------------------------------------- #
# Attaching to the browser that is already open
# --------------------------------------------------------------------------- #


def devtools_version(port: int) -> dict | None:
    """`/json/version` if DevTools answers on `port`, else None.

    The HTTP endpoint is probed rather than the TCP port: a plain Chrome with no
    `--remote-debugging-port` still holds 9222 and 404s every request, which
    otherwise surfaces as an opaque `connect_over_cdp` failure.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.0) as r:
            body = json.load(r)
        return body if "webSocketDebuggerUrl" in body else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def pick_page(browser, match: str | None):
    """The tab to walk: `match` if given, else the carrier host, else the first.

    `chrome://` and `devtools://` tabs are dropped -- Chrome's own omnibox popup
    is a real CDP target and would otherwise be chosen ahead of the form.
    """
    pages = [
        p
        for ctx in browser.contexts
        for p in ctx.pages
        if not p.url.startswith(("devtools://", "chrome://", "chrome-extension://"))
    ]
    if not pages:
        pytest.skip(f"attached to Chrome but no ordinary page is open (targets={len(pages)})")

    for page in pages:
        if match and match in page.url:
            return page, pages
    if match:
        pytest.skip(f"no open tab matches TB_LIVE_MATCH={match!r}; open={[p.url for p in pages]}")
    return pages[0], pages


# --------------------------------------------------------------------------- #
# Taps. One log line per contract crossing, nothing else changed.
# --------------------------------------------------------------------------- #


class Budget(Exception):
    """Look budget spent. Raised inside the perceiver to unwind the graph."""


class TappedFrontier(FrontierAgent):
    """Logs what Frontier was handed, what it decided, and the board after."""

    def on_page(self, job, page, scrape=None, fill_report=None):
        event(
            "loop>frontier",
            stage=page.stageId,
            controls=len(page.controls),
            polarity=scrape.polarity if scrape else None,
            report=fill_report.ok if fill_report else None,
            blockers=len(page.blockers),
        )
        outcome = super().on_page(job, page, scrape, fill_report)

        if isinstance(outcome, Walk):
            event("frontier>loop", decision="walk", paths=len(outcome.paths))
        elif isinstance(outcome, FillFieldAssignment):
            event(
                "frontier>loop",
                decision=outcome.type,
                field=outcome.fieldId,
                loc=outcome.locator,
                val=outcome.value,
            )
        elif isinstance(outcome, SetOptionAssignment):
            event(
                "frontier>loop",
                decision=outcome.type,
                field=outcome.fieldId,
                opt=outcome.option,
                loc=outcome.locator,
                ctl=outcome.controlLocator,
            )
        else:
            event("frontier>loop", decision=outcome.type)

        board = self.board
        explored = sum(1 for c in board.controls if c.explored)
        event(
            "board",
            status=board.status,
            stage=board.currentStageId,
            explored=f"{explored}/{len(board.controls)}",
            open=[c.fieldId for c in board.controls if not c.explored],
        )
        return outcome


class TappedFiller(StubFormFiller):
    """Logs the Assignment in and the FillReport out."""

    def execute(self, job, stage_id, assignment):
        event("loop>filler", type=assignment.type, field=getattr(assignment, "fieldId", None))
        report = super().execute(job, stage_id, assignment)
        event(
            "filler>loop",
            ok=report.ok,
            advance=report.advance,
            landed=report.landed,
            steps=len(report.steps),
            disc=None
            if report.discoveredOptions is None
            else [o.label for o in report.discoveredOptions],
            chosen=report.chosenOption,
            err=report.errorClass,
        )
        return report


def tapped_perceiver(page, settings, budget: int, looks: dict):
    """Wrap the real scraper so both sides of the Look contract are logged."""

    def perceiver(request: PerceiveRequest) -> ScraperResult:
        looks["n"] += 1
        if looks["n"] > budget:
            event("run.halt", reason="look budget spent", looks=budget)
            raise Budget(f"look budget {budget} spent")

        event(
            "loop>scraper",
            look=looks["n"],
            page_index=request.page_index,
            prior=request.prior.stageId if request.prior else None,
            assign=request.assignment,
        )
        started = time.monotonic()
        result = perceive(page, request.model_copy(update={"objective": OBJECTIVE}), settings)
        described = result.page
        event(
            "scraper>loop",
            look=looks["n"],
            stage=described.stageId,
            controls=len(described.controls),
            polarity=result.polarity,
            add=len(result.addedControls),
            rm=len(result.removedControls),
            chg=len(result.changedControls),
            next=described.next is not None,
            back=described.back is not None,
            blockers=len(described.blockers),
            ms=int((time.monotonic() - started) * 1000),
        )
        # Controls are logged on the first look and on any look that moved -- a
        # settled page repeating its control list adds bytes, not information.
        if looks["n"] == 1 or result.addedControls or result.removedControls or result.changedControls:
            for control in described.controls:
                event(
                    "control",
                    id=control.fieldId,
                    type=control.type,
                    req=control.required,
                    uniq=control.unique,
                    opts=None if control.options is None else [o.label for o in control.options],
                    loc=control.locator,
                    label=control.label,
                    by=control.revealedBy.fieldId if control.revealedBy else None,
                )
        for blocker in described.blockers:
            event("blocker", text=blocker)
        return result

    return perceiver


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def walk(log_path):
    """Attach, walk once, log everything. Returns (walk, frontier, looks, error, first)."""
    settings = get_settings()
    port = int(os.getenv("TB_LIVE_PORT") or settings.cdp_port)
    budget = int(os.getenv("TB_LIVE_LOOKS", "25"))
    match = os.getenv("TB_LIVE_MATCH") or (
        urlparse(settings.carrier_url).netloc if settings.carrier_url else None
    )

    version = devtools_version(port)
    if version is None:
        pytest.skip(
            f"nothing serving CDP on 127.0.0.1:{port}. Start Chrome with "
            f"--remote-debugging-port={port} --user-data-dir=<dir>, log in, "
            "and leave the form open."
        )

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page, pages = pick_page(browser, match)

        event(
            "run.start",
            cdp=port,
            chrome=version.get("Browser"),
            tabs=len(pages),
            url=page.url,
            title=page.title(),
            provider=settings.llm_provider,
            model=settings.openrouter_model
            if settings.llm_provider == "openrouter"
            else settings.anthropic_model,
            perceiver=settings.scraper_perceiver,
            filler="stub",
            budget=budget,
        )
        for other in pages:
            if other is not page:
                event("tab.skipped", url=other.url)

        looks = {"n": 0}
        frontier = TappedFrontier()
        started = time.monotonic()
        result: Walk = Walk()
        error: Exception | None = None

        perceiver = tapped_perceiver(page, settings, budget, looks)
        first = perceiver(PerceiveRequest(job_id=JOB, page_index=1, objective=OBJECTIVE))
        loop = Loop(perceiver, frontier, TappedFiller())

        try:
            result = loop.fill_form(JOB, first.page)
        except Budget as e:
            error = e
        except Exception as e:  # logged, then asserted on -- never swallowed
            error = e
            event("run.error", type=type(e).__name__, msg=str(e))
            log.exception("walk raised")
    finally:
        # The browser is the user's. Drop the CDP connection and leave it alone
        # -- browser.close() here would shut down their window mid-session.
        playwright.stop()

    for i, path in enumerate(result.paths, start=1):
        event("walk.path", n=i, steps=len(path.steps), choices=path.choices)
        for step in path.steps:
            event(
                "walk.step",
                path=i,
                action=step.action,
                field=step.fieldId,
                loc=step.locator,
                val=step.option or step.value,
            )

    board = frontier.board
    event(
        "run.end",
        ok=error is None,
        looks=looks["n"],
        paths=len(result.paths),
        actions=len(frontier.walk_log),
        controls=len(board.controls),
        explored=sum(1 for c in board.controls if c.explored),
        status=board.status,
        ms=int((time.monotonic() - started) * 1000),
    )
    return result, frontier, looks["n"], error, first


def test_live_walk(walk, log_path):
    """Loop -> Scraper -> Frontier holds against the live page, and terminates."""
    result, frontier, looks, error, first = walk
    where = f"see {log_path}"

    assert error is None, f"walk did not finish: {error!r} ({where})"

    # Scraper: the page was described, and every locator is a verified count()==1.
    assert first.page.controls, f"scraper found no controls ({where})"
    assert first.polarity == "+ve", f"first look must be +ve, got {first.polarity} ({where})"
    not_unique = [c.fieldId for c in first.page.controls if not c.unique]
    assert not not_unique, f"locators not unique: {not_unique} ({where})"

    # Frontier: it consumed the descriptions, explored every control, and stopped.
    board = frontier.board
    assert board.controls, f"frontier tracked no controls ({where})"
    assert board.status in ("complete", "slice_stable"), (
        f"walk ended at status={board.status}, expected complete/slice_stable ({where})"
    )
    unexplored = [c.fieldId for c in board.controls if not c.explored]
    assert not unexplored, f"controls left unexplored: {unexplored} ({where})"

    # Loop: the cycle terminated on its own with something replayable.
    assert result.paths, f"no replayable path published ({where})"
    assert all(p.steps for p in result.paths), f"a published path has no steps ({where})"
