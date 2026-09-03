"""Drive FormFiller against the fixture form and log every contract crossing.

    Assignment  ->  FormFiller  ->  FillReport

No Loop, no Frontier, no Scraper. The assignments below are hand-written — they
are what Frontier WOULD emit for this page — so what you see is the filler alone,
which is the point of building it on its own branch.

    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/demo_form_filler.py
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/demo_form_filler.py --rules
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/demo_form_filler.py --headed

By default the values come from the model (COSTS TOKENS, one call per distinct
field). `--rules` uses the offline table instead and costs nothing.
"""

import argparse
import datetime
import json
import logging
import pathlib
import sys

from playwright.sync_api import sync_playwright
from pydantic import BaseModel

from trailblazer.agents.form_filler.form_filler import FormFiller
from trailblazer.agents.form_filler.value_picker import LLMValuePicker, rule_based
from trailblazer.contracts import Assignment, Option, PageDescription

log = logging.getLogger("demo")
STEP = 0

FIXTURE = pathlib.Path("tests/fixtures/form_page.html").resolve()
JOB = "job_demo_filler"
STAGE = "form_page_1_business_info"

# What Frontier would emit for this page, in the order it would emit it: every
# control once, then each remaining option of every chooser it discovered, then
# Next. Written out rather than generated so the filler is the only thing under
# test here.
SCRIPT: list[Assignment] = [
    Assignment(type="fill_field", fieldId="q_001", locator="#agencyProgram"),
    Assignment(
        type="set_option",
        fieldId="q_001",
        option=Option(label="Pie Partner Program", locator='role=option[name="Pie Partner Program"]'),
        locator="#agencyProgram",
    ),
    Assignment(type="fill_field", fieldId="q_002", locator="#effectiveDate"),
    Assignment(type="fill_field", fieldId="q_003", locator="#businessZipCode"),
    Assignment(type="fill_field", fieldId="q_004", locator="#businessName"),
    Assignment(type="fill_field", fieldId="q_006", locator="#entityType"),
    Assignment(
        type="set_option",
        fieldId="q_006",
        option=Option(label="Corporation", locator='#entityType >> option:text-is("Corporation")'),
        locator="#entityType",
    ),
    Assignment(type="fill_field", fieldId="q_007", locator="#fein"),
    Assignment(type="fill_field", fieldId="q_008", locator="#targetPremium"),
    Assignment(
        type="set_option",
        fieldId="q_009",
        option=Option(label="No", locator="#locationsNo"),
        locator="#locationsYes",
    ),
    Assignment(type="fill_field", fieldId="q_010", locator="#priorCoverage"),
    # Deliberately wrong, to show what a locator miss reports.
    Assignment(type="fill_field", fieldId="q_404", locator="#noSuchField"),
    Assignment(type="next"),
]

PAGE = PageDescription(
    stageId=STAGE,
    url=FIXTURE.as_uri(),
    controls=[],
    next="#realNext",
    back=None,
)


def setup_logging() -> pathlib.Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = pathlib.Path("logs")
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"formfiller-{stamp}.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-34s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(path, encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    # The HTTP client narrates every model call; the value picker already logs
    # what it decided, which is the part worth reading.
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def dump(name: str, obj) -> None:
    """One contract object as pretty JSON, numbered in flow order."""
    global STEP
    STEP += 1
    payload = obj.model_dump() if isinstance(obj, BaseModel) else obj
    banner = f"[{STEP:02d}] {name}"
    log.info("")
    log.info("--- %s %s", banner, "-" * max(0, 66 - len(banner)))
    for line in json.dumps(payload, indent=2, default=str).splitlines():
        log.info("    %s", line)


def read_form(page) -> tuple[dict, str]:
    """Every value the form currently holds, straight off the DOM."""
    values = page.evaluate(
        """() => Object.fromEntries(
             [...document.querySelectorAll('#app-form input, #app-form select')]
               .filter(el => el.id)
               .map(el => [el.id, el.type === 'checkbox' ? String(el.checked) : el.value])
           )"""
    )
    return values, page.inner_text("#agencyProgram")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rules",
        action="store_true",
        help="use the offline value table instead of the model (free)",
    )
    parser.add_argument("--headed", action="store_true", help="watch the browser work")
    args = parser.parse_args()

    log_path = setup_logging()
    picker = rule_based if args.rules else LLMValuePicker()

    log.info("=" * 78)
    log.info("  FORMFILLER - %s", FIXTURE.name)
    log.info("  values from: %s", "rule table" if args.rules else "model")
    log.info("=" * 78)

    reports = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        page.goto(FIXTURE.as_uri())

        filler = FormFiller(page, value_picker=picker)
        final, widget = {}, ""
        for assignment in SCRIPT:
            # Read the form back before anything navigates away from it. The
            # reports say what landed; this is the page's own account, and the
            # two have to agree.
            if assignment.type in ("next", "back", "submit"):
                final, widget = read_form(page)

            dump(f"Assignment [{assignment.type}]", assignment)
            report = filler.execute(JOB, STAGE, assignment, page_description=PAGE)
            dump("FillReport", report)
            reports.append((assignment, report))

        if not final:
            final, widget = read_form(page)
        browser.close()

    log.info("")
    log.info("  WHAT THE PAGE HOLDS NOW")
    log.info("  %-20s %s", "agencyProgram", widget)
    for field_id, value in final.items():
        log.info("  %-20s %s", field_id, value or "-")

    ok = sum(1 for _, r in reports if r.ok)
    log.info("")
    log.info("  %d assignments, %d ok, %d failed", len(reports), ok, len(reports) - ok)
    for assignment, report in reports:
        if not report.ok:
            log.info("  failed: %s %s -> %s", assignment.type, assignment.fieldId, report.errorClass)
    log.info("  full trace: %s", log_path)
    log.info("=" * 78)


if __name__ == "__main__":
    main()
