"""The live FormFiller run, but with q_006's options already known.

A copy of demo_form_filler_live.py with ONE difference: q_006 "Legal Entity
Type" arrives with its five options filled in instead of `options: null`.

    demo_form_filler_live.py        options: null  -> fill_field  -> the filler
                                    opens the widget and DISCOVERS the options
    this script                     options: [...] -> set_option x5, no
                                    fill_field at all, nothing discovered

Which is the other half of the contract. Scraper reports `null` for a widget
that renders nothing until opened, and `[..]` when it could read the list up
front; Frontier goes straight to set_option in the second case, so the filler is
handed a label and never gets to look at the control first.

The options carry NO locator of their own -- `locator: ""` -- because a plain
dropdown does not need one. `Assignment.action_locator` is built for exactly
this: it returns `option.locator or locator`, falling back to the parent
control's. So every set_option here acts on `#entityType` and the label is the
only thing distinguishing one assignment from the next.

    # 1. close Chrome, then relaunch it with the debugging port open:
    & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `
        --remote-debugging-port=9456 --user-data-dir="$env:LOCALAPPDATA\\Temp\\pie-cdp"
    # 2. in that Chrome: log into partner.pieinsurance.com and navigate to the
    #    work-comp business-info form.
    # 3. run this:
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/demo_form_filler_live_known_options.py
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/demo_form_filler_live_known_options.py --only q_006
    $env:PYTHONPATH = "."; .venv/Scripts/python.exe scripts/demo_form_filler_live_known_options.py --rules

This WRITES to the real form: it types test values and clicks real dropdowns.
It does NOT click Next unless you pass --with-next.
"""

import argparse
import datetime
import json
import logging
import pathlib
import sys

from playwright.sync_api import sync_playwright
from pydantic import BaseModel
##
from trailblazer.agents.form_filler.form_filler import FormFiller
from trailblazer.agents.form_filler.value_picker import LLMValuePicker, rule_based
from trailblazer.contracts import Assignment, Option, PageDescription

log = logging.getLogger("demo")
STEP = 0

CDP_URL = "http://localhost:9456"
JOB = "job_demo_filler_live_known_options"
PAGE_URL_MARK = "business-info"

# The same PageDescription, with q_006's options supplied rather than null.
PAGE_JSON = r"""
{
  "stageId": "form_page_1_business_info",
  "url": "https://partner.pieinsurance.com/work-comp/business-info",
  "controls": [
    { "fieldId": "q_001", "label": "Agency / Program", "type": "other", "required": true, "options": null, "locator": "#agencyProgram", "unique": true, "revealedBy": null },
    { "fieldId": "q_002", "label": "Policy Effective Date", "type": "date", "required": true, "options": null, "locator": "#effectiveDate", "unique": true, "revealedBy": null },
    { "fieldId": "q_003", "label": "Business Zip Code", "type": "text", "required": true, "options": null, "locator": "#businessZipCode", "unique": true, "revealedBy": null },
    { "fieldId": "q_004", "label": "Legal Business Name", "type": "text", "required": true, "options": null, "locator": "#businessName", "unique": true, "revealedBy": null },
    { "fieldId": "q_005", "label": "DBA (Doing Business As)", "type": "text", "required": false, "options": null, "locator": "#dba-0", "unique": true, "revealedBy": null },
    { "fieldId": "q_006", "label": "Legal Entity Type", "type": "other", "required": true,
      "options": [
        { "label": "Corporation", "locator": "" },
        { "label": "Partnership", "locator": "" },
        { "label": "Limited Liability Company", "locator": "" },
        { "label": "Sole Proprietorship", "locator": "" },
        { "label": "Other", "locator": "" }
      ],
      "locator": "#entityType", "unique": true, "revealedBy": null },
    { "fieldId": "q_007", "label": "FEIN", "type": "text", "required": true, "options": null, "locator": "#fein", "unique": true, "revealedBy": null },
    { "fieldId": "q_008", "label": "Target or Incumbent Premium", "type": "number", "required": false, "options": null, "locator": "#targetPremium", "unique": true, "revealedBy": null },
    { "fieldId": "q_009", "label": "Does this business have multiple locations?", "type": "select", "required": true,
      "options": [
        { "label": "Yes", "locator": "internal:label=\"Yes\"i" },
        { "label": "No", "locator": "internal:label=\"No\"i" }
      ],
      "locator": "internal:label=\"Yes\"i", "unique": true, "revealedBy": null }
  ],
  "next": "button:has-text(\"Next\")",
  "back": null,
  "candidateGates": ["q_009"],
  "blockers": []
}
"""

PAGE = PageDescription.model_validate_json(PAGE_JSON)


def setup_logging() -> pathlib.Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = pathlib.Path("logs")
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"formfiller-live-known-options-{stamp}.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-34s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(path, encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def dump(name: str, obj) -> None:
    global STEP
    STEP += 1
    payload = obj.model_dump() if isinstance(obj, BaseModel) else obj
    banner = f"[{STEP:02d}] {name}"
    log.info("")
    log.info("--- %s %s", banner, "-" * max(0, 66 - len(banner)))
    for line in json.dumps(payload, indent=2, default=str).splitlines():
        log.info("    %s", line)


def attach_to_pie_tab(playwright):
    """Find the already-open Pie business-info tab in the debugged Chrome."""
    browser = playwright.chromium.connect_over_cdp(CDP_URL)
    pages = [p for ctx in browser.contexts for p in ctx.pages]
    if not pages:
        raise SystemExit(f"connected to {CDP_URL} but it has no open tabs")

    for page in pages:
        if PAGE_URL_MARK in page.url:
            log.info("attached to: %s", page.url)
            page.bring_to_front()
            return browser, page

    listing = "\n  ".join(p.url for p in pages)
    raise SystemExit(
        f"no tab whose URL contains {PAGE_URL_MARK!r}. Open tabs:\n  {listing}"
    )


def assignments_for(control) -> list[Assignment]:
    """What Frontier would emit for one control, before discovery.

    A control Scraper already gave options for goes straight to set_option, one
    per option. Everything else is a fill_field -- which doubles as discovery.
    """
    if control.options:
        return [
            Assignment(type="set_option", fieldId=control.fieldId,
                       option=opt, locator=control.locator)
            for opt in control.options
        ]
    return [Assignment(type="fill_field", fieldId=control.fieldId, locator=control.locator)]


def remaining_option_assignments(control, report) -> list[Assignment]:
    """Loop's job: a fill_field came back a chooser -> walk the rest.

    Mirrors Frontier.board: skip the option the filler already chose.
    """
    if not report.discoveredOptions:
        return []
    return [
        Assignment(type="set_option", fieldId=control.fieldId,
                   option=opt, locator=control.locator)
        for opt in report.discoveredOptions
        if opt.label != report.chosenOption
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", action="store_true",
                        help="offline value table instead of the model (free)")
    parser.add_argument("--only", metavar="FIELD_ID",
                        help="run just this one control (e.g. q_004)")
    parser.add_argument("--with-next", action="store_true",
                        help="also click Next at the end (navigates away!)")
    args = parser.parse_args()

    log_path = setup_logging()
    picker = rule_based if args.rules else LLMValuePicker()

    controls = PAGE.controls
    if args.only:
        controls = [c for c in controls if c.fieldId == args.only]
        if not controls:
            raise SystemExit(f"no control {args.only!r} in the page description")

    log.info("=" * 78)
    log.info("  FORMFILLER (LIVE, q_006 options known) - %s", PAGE.url)
    log.info("  values from: %s", "rule table" if args.rules else "model")
    log.info("  controls: %s", ", ".join(c.fieldId for c in controls))
    log.info("=" * 78)

    reports = []
    with sync_playwright() as playwright:
        browser, page = attach_to_pie_tab(playwright)
        filler = FormFiller(page, value_picker=picker)

        for control in controls:
            queue = assignments_for(control)
            discovered_followups_done = False
            while queue:
                assignment = queue.pop(0)
                dump(f"Assignment [{assignment.type}] {assignment.fieldId}", assignment)
                report = filler.execute(JOB, PAGE.stageId, assignment, page_description=PAGE)
                dump("FillReport", report)
                reports.append((assignment, report))

                if (assignment.type == "fill_field" and not discovered_followups_done):
                    discovered_followups_done = True
                    queue.extend(remaining_option_assignments(control, report))

        if args.with_next:
            nav = Assignment(type="next")
            dump("Assignment [next]", nav)
            report = filler.execute(JOB, PAGE.stageId, nav, page_description=PAGE)
            dump("FillReport", report)
            reports.append((nav, report))

        browser.close()  # detaches CDP; does NOT close the user's Chrome

    ok = sum(1 for _, r in reports if r.ok)
    log.info("")
    log.info("  %d assignments, %d ok, %d failed", len(reports), ok, len(reports) - ok)
    for assignment, report in reports:
        if not report.ok:
            log.info("  failed: %s %s -> %s",
                     assignment.type, assignment.fieldId, report.errorClass)
    log.info("  full trace: %s", log_path)
    log.info("=" * 78)


if __name__ == "__main__":
    main()
