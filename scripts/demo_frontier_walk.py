"""
Trace the entire flow through a Frontier walk — every contract object and every
agent's internal decision, in order.

Fully offline: stub Scraper, stub FormFiller, no LLM, no browser. This is the
quickest way to see the whole protocol at once:

    Frontier -> Assignment -> FormFiller -> FillReport -> Loop -> Scraper -> Diff -> Frontier

Usage:
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/demo_frontier_walk.py basic
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/demo_frontier_walk.py simple
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/demo_frontier_walk.py         # pie

Scenarios:
    basic    Name / Gender / Email — three fields, nothing else
    simple   the above plus a Yes/Maybe control, a second page, a revealed field
    pie      (default) the real live Pie Insurance business-info scrape

Every run also writes the full trace to logs/<scenario>-<timestamp>.log.
"""

import argparse
import datetime
import json
import logging
import pathlib
import sys

from pydantic import BaseModel

from trailblazer.agents.form_filler.stub import StubFormFiller
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.stub import StubScraper
from trailblazer.contracts import Control, Option, PageDescription
from trailblazer.loop.orchestrator import Loop
from tests.agents.frontier.frontier_test_data import (
    GENDER_OPTIONS,
    PAGE_1_BUSINESS_INFO,
    PAGE_NAME_GENDER_EMAIL,
    PAGE_SIMPLE,
    PAGE_SIMPLE_2,
    PIE_DISCOVERABLE,
    PIE_REVEALED_LOCATION_COUNT,
    REVEALED_PRONOUNS,
)

log = logging.getLogger("trace")
STEP = 0


# ---------------------------------------------------------------------------
# Logging: everything goes to stdout AND to a file, so the whole flow is kept
# ---------------------------------------------------------------------------


class _ShortNameFormatter(logging.Formatter):
    """Trim the `trailblazer.` package prefix so the message column starts early."""

    def format(self, record):
        record.name = (
            record.name.replace("trailblazer.agents.", "")
            .replace("trailblazer.", "")
            .replace("form_filler.", "")
            .replace("scraper.", "")
            .replace("frontier.frontier", "frontier")
        )
        return super().format(record)


def setup_logging(scenario: str) -> pathlib.Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = pathlib.Path("logs")
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"{scenario}-{stamp}.log"

    fmt = _ShortNameFormatter("%(asctime)s %(levelname)-7s %(name)-20s %(message)s", "%H:%M:%S")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    to_file = logging.FileHandler(path, encoding="utf-8")
    to_file.setFormatter(fmt)
    root.addHandler(to_file)

    return path


def log_contract(name: str, obj) -> None:
    """Log one contract object as pretty JSON, numbered in flow order."""
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
    log.info("--- %s %s", banner, "-" * max(0, 62 - len(banner)))
    for line in json.dumps(payload, indent=2, default=str).splitlines():
        log.info("    %s", line)


# ---------------------------------------------------------------------------
# Tracing wrappers — log each contract as it crosses an agent boundary
# ---------------------------------------------------------------------------


class TracingScraper(StubScraper):
    """
    Log every look, but dump the PageDescription in full only when it changed.

    A walk looks at the page after every single fill, and re-printing an
    identical 60-line payload a dozen times isn't more information — the -ve
    diff already says nothing changed. So: full payload on the first look and
    whenever it differs, one line otherwise.
    """

    _last: str | None = None

    def look(self, job, objective="perceive", last_assignment=None, fill_report=None):
        page, diff = super().look(job, objective, last_assignment, fill_report)

        current = page.model_dump_json()
        if current != self._last:
            log_contract(f"Scraper -> PageDescription [{page.stageId}]", page)
            self._last = current
        else:
            global STEP
            STEP += 1
            log.info("")
            log.info(
                "--- [%02d] Scraper -> PageDescription [%s] unchanged (%d controls)",
                STEP,
                page.stageId,
                len(page.controls),
            )

        log_contract("Scraper -> Diff", diff)
        return page, diff


class TracingFiller(StubFormFiller):
    def execute(self, job, stage_id, assignment):
        log_contract(f"Frontier -> Assignment [{assignment.type}]", assignment)
        report = super().execute(job, stage_id, assignment)
        log_contract("FormFiller -> FillReport", report)
        return report


class TracingFrontier(FrontierAgent):
    """
    Log the board after every decision, so state evolution is visible.

    As a compact table rather than JSON: the board is dumped on every one of the
    ~30 calls in a walk, and nine controls of pretty-printed JSON each time
    buries the assignments and fill reports between them.
    """

    def on_page(self, job, page, diff=None, fill_report=None):
        outcome = super().on_page(job, page, diff, fill_report)

        global STEP
        STEP += 1
        banner = f"[{STEP:02d}] Frontier board [{self.board.status}] {self.board.currentStageId}"
        log.info("")
        log.info("--- %s %s", banner, "-" * max(0, 62 - len(banner)))
        for c in self.board.controls:
            if c.options is None:
                options = "options=?"
            elif not c.options:
                options = "options=none"
            else:
                options = "walked=[%s] pending=[%s]" % (
                    ",".join(o.label for o in c.walked),
                    ",".join(o.label for o in c.pending),
                )
            log.info(
                "    %s %-8s %s",
                "[x]" if c.explored else "[ ]",
                c.fieldId,
                options,
            )
        return outcome


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def pie_scenario(with_reveal: bool):
    """The real live Pie Insurance business-info scrape."""
    pages = [PageDescription(**PAGE_1_BUSINESS_INFO)]

    # q_001 and q_006 are custom dropdowns: Scraper reported options: null
    # because they don't render their list until opened. FormFiller finds them.
    discoverable = {
        field_id: [Option(**o) for o in options]
        for field_id, options in PIE_DISCOVERABLE.items()
    }

    # Answering "Does this business have multiple locations?" = Yes reveals a
    # count field, which must be explored before the page is done.
    reveals = (
        {("q_009", "Yes"): [Control(**PIE_REVEALED_LOCATION_COUNT)]}
        if with_reveal
        else {}
    )
    return pages, discoverable, reveals, PAGE_1_BUSINESS_INFO.get("_meta")


def simple_scenario(with_reveal: bool):
    """Name / Gender / Email, plus a Yes/Maybe control and a revealed field."""
    pages = [PageDescription(**PAGE_SIMPLE), PageDescription(**PAGE_SIMPLE_2)]
    discoverable = {
        "q_gender": [
            Option(label="Male", locator="#gender-male"),
            Option(label="Female", locator="#gender-female"),
        ]
    }
    reveals = (
        {("q_gender", "Male"): [Control(**REVEALED_PRONOUNS)]} if with_reveal else {}
    )
    return pages, discoverable, reveals, None


def basic_scenario(with_reveal: bool):
    """Name / Gender / Email — the three-field walkthrough, nothing else."""
    pages = [PageDescription(**PAGE_NAME_GENDER_EMAIL)]
    discoverable = {"q_gender": [Option(**o) for o in GENDER_OPTIONS]}
    return pages, discoverable, {}, None


SCENARIOS = {
    "basic": basic_scenario,
    "simple": simple_scenario,
    "pie": pie_scenario,
}


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", default="pie", choices=list(SCENARIOS))
    parser.add_argument(
        "--no-reveal",
        action="store_true",
        help="don't simulate a control revealing new fields",
    )
    args = parser.parse_args()

    log_path = setup_logging(args.scenario)
    pages, discoverable, reveals, meta = SCENARIOS[args.scenario](not args.no_reveal)

    log.info("=" * 78)
    log.info("  FRONTIER WALK - scenario=%s", args.scenario)
    log.info("=" * 78)

    if meta:
        # Provenance from the real scraper. Not part of the PageDescription
        # contract (Pydantic drops it), but worth recording in the trace.
        log_contract("PageDescription._meta (provenance, dropped by the contract)", meta)

    log_contract("initial PageDescription (Scraper output)", pages[0])
    if discoverable:
        log_contract(
            "controls FormFiller will discover are dropdowns",
            {k: [o.model_dump() for o in v] for k, v in discoverable.items()},
        )
    if reveals:
        log_contract(
            "reveals scripted into the stub Scraper",
            {f"{f}={o}": [c.fieldId for c in cs] for (f, o), cs in reveals.items()},
        )

    frontier = TracingFrontier()
    loop = Loop(
        TracingScraper(pages, reveals=reveals),
        frontier,
        TracingFiller(discoverable=discoverable),
        recursion_limit=200,
    )

    walk = loop.fill_form("job_demo", pages[0])

    log_contract("Frontier -> ReplayGen: Walk", walk)

    # ---- summary -----------------------------------------------------------
    observed = frontier.walk_log
    log.info("")
    log.info("=" * 78)
    log.info(
        "  OBSERVED - %d actions in place, status=%s",
        len(observed),
        frontier.board.status,
    )
    log.info("  (what happened on the page; branches interleaved, NOT replayable)")
    log.info("=" * 78)
    for i, step in enumerate(observed, 1):
        log.info(
            "  %2d. %-7s %-10s %-44s %s",
            i,
            step.action,
            step.fieldId or "-",
            step.locator,
            step.option or step.value or "",
        )

    log.info("")
    log.info("=" * 78)
    log.info("  %d REPLAYABLE PATHS - one per branch, each a complete script", len(walk.paths))
    log.info("=" * 78)
    for n, path in enumerate(walk.paths, 1):
        pins = ", ".join(f"{f}={o}" for f, o in path.choices.items()) or "(no branches)"
        log.info("")
        log.info("  PATH %d/%d  %s", n, len(walk.paths), pins)
        for i, step in enumerate(path.steps, 1):
            log.info(
                "    %2d. %-7s %-10s %-42s %s",
                i,
                step.action,
                step.fieldId or "-",
                step.locator,
                step.option or step.value or "",
            )

    log.info("")
    log.info("  BOARD")
    for c in frontier.board.controls:
        options = "-" if c.options is None else (",".join(o.label for o in c.options) or "(none)")
        log.info(
            "  %-8s explored=%-5s walked=%-2d pending=%-2d options=%s",
            c.fieldId,
            c.explored,
            len(c.walked),
            len(c.pending),
            options,
        )

    unexplored = [c.fieldId for c in frontier.board.controls if not c.explored]
    log.info("")
    log.info("  unexplored: %s", unexplored or "none")
    log.info("  full trace: %s", log_path)
    log.info("=" * 78)


if __name__ == "__main__":
    main()
