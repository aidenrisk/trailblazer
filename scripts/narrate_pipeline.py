"""Run the real pipeline and narrate every step in plain English.

Same machinery as `live_pipeline.py`, but the output reads as a story instead of
JSON: who was called, what they said, what Frontier decided and *why* it decided
it. Use this to watch behaviour; use `live_pipeline.py --attach` when you want
the raw contract payloads.

    Scraper  -- reads the page
    Frontier -- picks ONE control and one action
    FormFiller -- does it (stub for now: reports, does not touch the page)
    Loop     -- carries the report back to Frontier and asks the scraper to look again

Attach to a browser you have already logged into (needs Chrome started with
--remote-debugging-port, and the port free before Chrome starts):

    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/narrate_pipeline.py --attach

Or let it launch its own browser and hold while you log in:

    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/narrate_pipeline.py --wait-for business-info

Or run against the local fixture form, free of login and cheap:

    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/narrate_pipeline.py --fixture

COSTS TOKENS: one LLM call per look. --max-looks caps it.
"""

import argparse
import datetime
import logging
import pathlib
import sys
import time

from playwright.sync_api import sync_playwright

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.form_filler.stub import StubFormFiller
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.contracts import (
    Assignment,
    FillFieldAssignment,
    FillReport,
    PerceiveRequest,
    ScraperResult,
    SetOptionAssignment,
    SimpleAssignment,
    Walk,
)
from trailblazer.loop.orchestrator import Loop
from trailblazer.shared.config import get_settings

FIXTURE = pathlib.Path("tests/fixtures/form.html")

log = logging.getLogger("story")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_STEP = 0


def beat(who: str, headline: str) -> None:
    """Start a new numbered step in the story."""
    global _STEP
    _STEP += 1
    log.info("")
    log.info("%s", "-" * 76)
    log.info("[%02d] %-10s  %s", _STEP, who.upper(), headline)
    log.info("%s", "-" * 76)


def say(text: str = "", indent: int = 5) -> None:
    log.info("%s%s", " " * indent, text)


def quantity(n: int, one: str, many: str | None = None) -> str:
    return f"1 {one}" if n == 1 else f"{n} {many or one + 's'}"


class Cap(Exception):
    """Stop the walk once the look budget is spent."""


# ---------------------------------------------------------------------------
# Narrating the three agents
# ---------------------------------------------------------------------------


def describe_control(c) -> str:
    """A control in words."""
    bits = [f'"{c.label}" ({c.fieldId}, a {c.type} field)']
    if c.required:
        bits.append("required")
    return ", ".join(bits)


def describe_state(c) -> str:
    """What Frontier currently knows about one control."""
    if c.explored:
        if c.walked:
            return f"done - tried all {quantity(len(c.walked), 'choice')}: " + ", ".join(
                o.label for o in c.walked
            )
        return "done - filled once"
    if c.pending:
        left = ", ".join(o.label for o in c.pending)
        if not c.walked:
            return f"not touched yet; {quantity(len(c.pending), 'choice')} to try: {left}"
        tried = ", ".join(o.label for o in c.walked)
        return f"part-way - tried {tried}; still to try: {left}"
    if c.options is None:
        return "not touched yet; nobody knows if it has choices"
    return "not touched yet"


def why_this_one(frontier, control) -> str:
    """Frontier's reason for picking this control, in words."""
    if control.revealedBy is not None and not control.explored:
        revealer = next(
            (c for c in frontier.board.controls if c.fieldId == control.revealedBy.fieldId),
            None,
        )
        if revealer and revealer.walked and revealer.walked[-1].label == control.revealedBy.equals:
            return (
                f'it only exists while "{control.revealedBy.fieldId}" is set to '
                f'"{control.revealedBy.equals}", which it is RIGHT NOW - so it has to '
                f"be done before that changes and the field vanishes"
            )
    if control.pending:
        return (
            f"it still has {quantity(len(control.pending), 'untried choice')} left, and "
            f"Frontier never moves past a control that is not finished"
        )
    return "it is the first control on this page that has not been touched"


def describe_assignment(a: Assignment) -> list[str]:
    """The instruction Frontier is sending, in words."""
    if isinstance(a, FillFieldAssignment):
        return [
            f'Type "{a.value}" into {a.fieldId}.',
            f"Target: {a.locator}",
            "If it turns out to be a dropdown, say so in the report.",
        ]
    if isinstance(a, SetOptionAssignment):
        if a.locator is not None:
            return [
                f'Choose "{a.option}" for {a.fieldId}.',
                f"That choice has its own element, so CLICK it: {a.locator}",
            ]
        return [
            f'Choose "{a.option}" for {a.fieldId}.',
            "That choice has no element of its own (a native <select>), so call",
            f"select_option(\"{a.option}\") on the control itself: {a.controlLocator}",
        ]
    if isinstance(a, SimpleAssignment):
        return {
            "next": ["Click Next - everything on this page is explored."],
            "back": ["Click Back."],
            "submit": ["Submit the form."],
            "stop": ["Stop. Nothing further can be done here."],
        }.get(a.type, [f"Do: {a.type}"])
    return [f"Do: {a.type}"]


def describe_report(r: FillReport, a: Assignment | None) -> list[str]:
    """What FormFiller reports back, in words."""
    if not r.ok:
        return [f"FAILED: {r.errorClass}. The walk stops here."]

    out = []
    if isinstance(a, FillFieldAssignment):
        if r.discoveredOptions is None:
            out.append(f'Typed "{a.value}" in. It was an ordinary field, no hidden choices.')
        elif not r.discoveredOptions:
            out.append("Opened it - it is a chooser but has no choices at all.")
        else:
            labels = ", ".join(o.label for o in r.discoveredOptions)
            out.append("SURPRISE: that was not a plain field, it is a dropdown.")
            out.append(f"Its choices are: {labels}")
            out.append(f'Picked "{r.chosenOption}" for now. Frontier needs to try the rest.')
    elif isinstance(a, SetOptionAssignment):
        step = r.steps[0] if r.steps else None
        how = "clicked" if step and step.action == "click" else "selected"
        out.append(f'{how.capitalize()} "{r.chosenOption}".')
    elif isinstance(a, SimpleAssignment):
        out.append(f"Did: {a.type}." + (" The page navigated." if r.advance else ""))
    return out


class NarratedFrontier(FrontierAgent):
    def on_page(self, job, page, scrape=None, fill_report=None):
        before = {c.fieldId: (c.explored, len(c.pending)) for c in self.board.controls}
        last = self._last_assignment

        beat("frontier", "What should we do next?")

        if scrape is None and fill_report is None:
            say("This is the first look. Nothing to learn from yet.")
        else:
            if fill_report is not None:
                for line in describe_report(fill_report, last):
                    say(f"Heard from FormFiller: {line}")
            if scrape is not None:
                if scrape.polarity == "+ve":
                    say(f"The scraper says the page CHANGED.")
                    if scrape.addedControls:
                        say(f"  New controls appeared: {', '.join(scrape.addedControls)}")
                    if scrape.removedControls:
                        say(f"  Controls disappeared: {', '.join(scrape.removedControls)}")
                else:
                    say("The scraper says the page did not change.")

        outcome = super().on_page(job, page, scrape, fill_report)

        # What did that teach it?
        newly = [
            c
            for c in self.board.controls
            if c.fieldId not in before
        ]
        finished = [
            c
            for c in self.board.controls
            if c.fieldId in before and not before[c.fieldId][0] and c.explored
        ]
        if newly:
            say()
            say(f"Now tracking {quantity(len(newly), 'new control')}: "
                + ", ".join(c.fieldId for c in newly))
        for c in finished:
            say()
            say(f'Marked "{c.label}" ({c.fieldId}) as DONE - {describe_state(c)}')

        say()
        done = sum(1 for c in self.board.controls if c.explored)
        say(f"Progress on this page: {done} of {len(self.board.controls)} controls done")
        for c in self.board.controls:
            mark = "x" if c.explored else " "
            say(f"  [{mark}] {c.fieldId:8s} {c.label[:26]:26s} {describe_state(c)}", indent=5)

        say()
        if isinstance(outcome, Walk):
            say(f"Nothing left anywhere. THE WALK IS OVER.")
            say(f"Publishing {quantity(len(outcome.paths), 'replayable path')} for ReplayGen.")
        elif isinstance(outcome, SimpleAssignment) and outcome.type in ("next", "stop", "submit"):
            for line in describe_assignment(outcome):
                say(f"Decision: {line}")
        else:
            target = next(
                (c for c in self.board.controls if c.fieldId == outcome.fieldId), None
            )
            if target is not None:
                say(f'Picked "{target.label}" ({target.fieldId}) because {why_this_one(self, target)}.')
            say()
            lines = describe_assignment(outcome)
            say(f"Decision: {lines[0]}")
            for line in lines[1:]:
                say(f"          {line}")
        return outcome


class NarratedFiller(StubFormFiller):
    def execute(self, job, stage_id, assignment):
        beat("formfiller", "Carrying out that one instruction")
        report = super().execute(job, stage_id, assignment)
        for line in describe_report(report, assignment):
            say(line)
        say()
        say("NOTE: this is the stub filler. It reports what it WOULD do but does")
        say("      not touch the page, so the form never actually changes.")
        return report


# ---------------------------------------------------------------------------


def run(page, args, settings) -> int:
    frontier = NarratedFrontier()
    looks = {"n": 0}

    def perceiver(request: PerceiveRequest) -> ScraperResult:
        looks["n"] += 1
        if looks["n"] > args.max_looks:
            raise Cap(f"look budget of {args.max_looks} spent")

        beat("scraper", "Reading the page")
        if request.assignment:
            for field, value in request.assignment.items():
                say(f'Told: "{field} was just set to {value}" (so new fields can be attributed)')
        else:
            say("Told: nothing was submitted before this look.")
        say(f"This is look {looks['n']}, page {request.page_index}.")

        result = perceive(page, request, settings)

        say()
        say(f'Read the page: "{result.page.stageId}"')
        say(f"Found {quantity(len(result.page.controls), 'control')}.")
        known = [c for c in result.page.controls if c.options]
        if known:
            verb = "lists its" if len(known) == 1 else "list their"
            say(f"{quantity(len(known), 'control')} already {verb} choices:")
            for c in known:
                how = (
                    "each choice is its own clickable element"
                    if all(o.locator for o in c.options)
                    else "the choices have no elements of their own"
                )
                say(f'  "{c.label}": {", ".join(o.label for o in c.options)}  ({how})')
        unknown = [c for c in result.page.controls if c.options is None and c.type in ("select", "toggle", "other")]
        if unknown:
            say(
                f"{quantity(len(unknown), 'control')} might be a dropdown but "
                f"{'did' if len(unknown) == 1 else 'did'} not render "
                f"{'its' if len(unknown) == 1 else 'their'} choices - FormFiller "
                "will have to find out:"
            )
            for c in unknown:
                say(f'  "{c.label}" ({c.fieldId})')
        if result.page.blockers:
            for b in result.page.blockers:
                say(f"WARNING - something is in the way: {b}")
        say(f"Compared to the previous look: {'CHANGED' if result.polarity == '+ve' else 'no change'}")
        return result

    beat("loop", "Starting. First, look at the page.")
    say(f"Page: {page.url}")

    first = perceiver(
        PerceiveRequest(
            job_id="story",
            page_index=1,
            objective="Describe this form page so every control can be filled.",
        )
    )

    walk = None
    loop = Loop(perceiver, frontier, NarratedFiller(), recursion_limit=300)
    try:
        walk = loop.fill_form("story", first.page)
    except Cap as e:
        log.info("")
        log.warning("STOPPED EARLY: %s", e)
        log.warning("Raise --max-looks to see more.")
    except Exception:
        log.exception("the pipeline raised")

    # ---- epilogue ----------------------------------------------------------
    log.info("")
    log.info("=" * 76)
    log.info("  HOW IT ENDED")
    log.info("=" * 76)
    say(f"Frontier's status: {frontier.board.status}")
    unexplored = [c.fieldId for c in frontier.board.controls if not c.explored]
    say(f"Controls left unexplored: {', '.join(unexplored) if unexplored else 'none'}")
    say(f"Looks spent: {looks['n']}")

    say()
    say(f"Everything that happened, in the order it happened ({len(frontier.walk_log)} actions):")
    for i, s in enumerate(frontier.walk_log, 1):
        what = {
            "type": f'typed "{s.value}"',
            "choose": f'chose "{s.option}"',
            "click": "clicked",
        }.get(s.action, s.action)
        where = s.locator or "(no element - select on the parent)"
        say(f"  {i:2d}. {what} into {s.fieldId or 'the page'}  [{where}]")

    if walk is not None:
        say()
        say(f"{quantity(len(walk.paths), 'replayable path')} to hand to ReplayGen.")
        say("Each is a complete script on its own - the branches are separated out,")
        say("so no script clicks two choices of the same dropdown.")
        for n, path in enumerate(walk.paths, 1):
            pins = ", ".join(f"{f}={o}" for f, o in path.choices.items()) or "no branches"
            say()
            say(f"  PATH {n} of {len(walk.paths)}  ({pins})")
            for i, s in enumerate(path.steps, 1):
                what = {
                    "type": f'type "{s.value}"',
                    "choose": f'choose "{s.option}"',
                    "click": "click",
                }.get(s.action, s.action)
                say(f"    {i:2d}. {what:34s} {s.locator or '(select on parent)'}")
    log.info("=" * 76)
    return 0


def setup_logging(out: str | None = None, story_only: bool = False) -> pathlib.Path:
    """Send the narration to stdout and to a file.

    `out` names the file; without it, a timestamped one under logs/. Either way
    logs/story-latest.log is refreshed to point at the newest run, so there is
    always one path to open without checking timestamps.
    """
    pathlib.Path("logs").mkdir(exist_ok=True)
    if out:
        path = pathlib.Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = pathlib.Path("logs") / f"story-{stamp}.log"

    story = logging.Formatter("%(message)s")
    detail = logging.Formatter("      . %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    class Fmt(logging.Formatter):
        def format(self, record):
            return (story if record.name == "story" else detail).format(record)

    class StoryOnly(logging.Filter):
        """Drop the agents' own log lines, leaving just the narration."""

        def filter(self, record):
            return record.name == "story"

    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(path, encoding="utf-8")]
    for h in handlers:
        h.setFormatter(Fmt())
        if story_only:
            h.addFilter(StoryOnly())
        root.addHandler(h)

    # The agents' own logging is useful but noisy next to the story.
    for noisy in ("httpx", "trailblazer.observability.cost"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # A stable filename for "the run I just did", so there is no timestamp to
    # look up. Written at the end of the run by `_mirror_latest`.
    return path


def _mirror_latest(path: pathlib.Path) -> pathlib.Path | None:
    """Copy the transcript to logs/story-latest.log."""
    latest = pathlib.Path("logs") / "story-latest.log"
    try:
        if path.resolve() != latest.resolve():
            latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        return latest
    except OSError as e:
        log.warning("could not write %s: %s", latest, e)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--attach", action="store_true", help="use a Chrome already on CDP_PORT")
    ap.add_argument("--fixture", action="store_true", help="use the local tests/fixtures/form.html")
    ap.add_argument("--url", default=None)
    ap.add_argument("--wait-for", default=None, metavar="SUBSTRING")
    ap.add_argument("--wait-timeout", type=int, default=300)
    ap.add_argument("--max-looks", type=int, default=30)
    ap.add_argument(
        "-o",
        "--out",
        default=None,
        metavar="PATH",
        help="write the transcript here (default: logs/story-<timestamp>.log). "
        "logs/story-latest.log always mirrors the newest run.",
    )
    ap.add_argument(
        "--story-only",
        action="store_true",
        help="leave the agents' own log lines out, keeping only the narration",
    )
    args = ap.parse_args()

    path = setup_logging(args.out, args.story_only)
    settings = get_settings()

    log.info("=" * 76)
    log.info("  THE PIPELINE, NARRATED")
    log.info("=" * 76)
    say(f"Scraper model : {settings.openrouter_model} (one call per look)")
    say(f"FormFiller    : stub - reports but does not touch the page")
    say(f"Look budget   : {args.max_looks}")
    say(f"Transcript    : {path}")

    try:
        return _main(args, settings, path)
    finally:
        latest = _mirror_latest(path)
        log.info("")
        log.info("  transcript: %s", path)
        if latest:
            log.info("  also at   : %s", latest)


def _main(args, settings, path) -> int:
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
                log.error("attached, but no page is open")
                return 4
            page = pages[0]
            if args.wait_for:
                page = next((p for p in pages if args.wait_for in p.url), page)
            say(f"Attached to your browser, using the tab at {page.url}")
            say("Not navigating anywhere - your session is left alone.")
            return run(page, args, settings)
        finally:
            pw.stop()

    url = args.url or (FIXTURE.resolve().as_uri() if args.fixture else settings.carrier_url)
    if not url:
        log.error("no URL: use --fixture, --url, --attach, or set CARRIER_URL")
        return 2

    with BrowserSession(cdp_port=settings.cdp_port, headed=bool(args.wait_for) or settings.headed) as session:
        page = session.goto(url)
        if args.wait_for:
            log.info("")
            log.info("  WAITING: log in in the window that just opened, then go to the")
            log.info("  page you want walked. Watching for a URL containing %r.", args.wait_for)
            log.info("  No LLM calls happen while waiting.")
            deadline = time.time() + args.wait_timeout
            seen = None
            while time.time() < deadline:
                current = page.url
                if current != seen:
                    say(f"  at: {current}")
                    seen = current
                if args.wait_for in current:
                    say("  found it - starting")
                    break
                time.sleep(2)
            else:
                log.error("timed out waiting for %r", args.wait_for)
                return 3
            page.wait_for_load_state("networkidle", timeout=15000)
        return run(page, args, settings)


if __name__ == "__main__":
    sys.exit(main())
