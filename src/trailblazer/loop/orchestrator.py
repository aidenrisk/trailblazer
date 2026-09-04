"""The deterministic Loop that glues the agents together.

A browser already serving CDP on `cdp_port` is attached to rather than replaced,
so one hand-authenticated headed browser serves every agent and every run.

`run_crawl` now runs the perceive -> Frontier -> assign cycle to page
completion. The form filler is not built, so an assignment is logged instead of
executed; without it the page never changes, and the walk finishes against a
static description. Generator and validator slot in around the same seam.
"""

import os
import uuid

from playwright.sync_api import Page

from trailblazer.agents.browser import shared_session
from trailblazer.agents.browser.session import AttachedSession, BrowserSession, devtools_running
from trailblazer.agents.form_filler.form_filler import fill_one
from trailblazer.agents.frontier import Frontier
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.contracts.assignment import Assignment, FillReport
from trailblazer.contracts.scraper_result import PerceiveRequest, ScraperResult
from trailblazer.observability.ledger import RunLedger
from trailblazer.observability.logging import get_logger, log_contract
from trailblazer.shared.config import Settings, get_settings

log = get_logger(__name__)

MAX_ASSIGNMENTS = 60
"""Assignments allowed on one page before the walk is abandoned.

A page that keeps producing assignments is a defect -- a blocker that never
clears, a control re-added under a new fieldId every perceive -- and looping on
it burns a model call per turn. Pie's widest page carries well under this.
"""


def fill(
    tab: Page,
    assignment: Assignment,
    settings: Settings,
    ledger: RunLedger | None = None,
) -> FillReport:
    """Run one assignment against the live tab."""
    return fill_one(tab, assignment, settings, ledger)


def open_session(settings: Settings, headed: bool = False):
    """Attach to the shared browser, or launch a private one if none is serving.

    The port comes from the record `trailblazer launch` wrote, so agents share
    one browser without being told where it is. Attaching leaves a hand-done
    login intact: `AttachedSession.close()` drops the connection only.
    """
    port = shared_session.live_port(settings.session_file, settings.cdp_port)
    if settings.attach_if_running:
        if devtools_running(port):
            return AttachedSession(cdp_port=port)
        # Launching here would produce a browser with no login, which perceives
        # a sign-in page instead of the form and reports it as the carrier's.
        raise RuntimeError(
            f"no shared browser serving CDP on port {port}. "
            "Run `trailblazer launch` and log in, then retry. "
            "(Set ATTACH_IF_RUNNING=false to launch a private browser instead.)"
        )
    return BrowserSession(
        cdp_port=port,
        headed=headed or settings.headed,
        profile_dir=os.path.expanduser(settings.browser_profile_dir),
    )


def perceive_once(
    url: str,
    page_index: int = 1,
    job_id: str | None = None,
    headed: bool = False,
    settings: Settings | None = None,
) -> ScraperResult:
    """Launch, navigate to `url`, perceive it once, and tear down."""
    settings = settings or get_settings()
    job = job_id or uuid.uuid4().hex[:12]

    with open_session(settings, headed) as session:
        page = session.goto(url)
        result = perceive(page, PerceiveRequest(job_id=job, page_index=page_index), settings)
    log_contract(log, "PageDescription", result.page)
    return result


def run_crawl(
    carrier_id: str,
    url: str,
    insurance_types: list[str],
    business_types: list[str],
    headed: bool = False,
    settings: Settings | None = None,
) -> ScraperResult:
    """Crawl one carrier portal and return the last thing the scraper saw.

    `insurance_types` and `business_types` reach Frontier, which matches them
    against a page's actions to pick which branch of a chooser page to take.
    """
    settings = settings or get_settings()
    job_id = uuid.uuid4().hex[:12]
    ledger = RunLedger(job_id=job_id)
    frontier = Frontier(
        business_types=business_types, insurance_types=insurance_types, ledger=ledger
    )
    log.info(
        "crawl start job_id=%s carrier_id=%s url=%s insurance_types=%s business_types=%s",
        job_id,
        carrier_id,
        url,
        ",".join(insurance_types),
        ",".join(business_types),
    )

    objective = (
        f"Describe this form page. The application is for {', '.join(insurance_types) or 'any'} "
        f"insurance for a {', '.join(business_types) or 'general'} business."
    )

    with open_session(settings, headed) as session:
        tab = session.goto(url)
        result = perceive(
            tab,
            PerceiveRequest(job_id=job_id, page_index=1, objective=objective),
            settings,
        )
        log_contract(log, "ScraperResult", result)
        result = _walk_page(tab, result, frontier, job_id, objective, settings, ledger)

    log.info(
        "crawl end job_id=%s stage_id=%s polarity=%s board=%s",
        job_id,
        result.page.stageId,
        result.polarity,
        frontier.summary(),
    )
    ledger.log_summary()
    return result


def _walk_page(
    tab,
    result: ScraperResult,
    frontier: Frontier,
    job_id: str,
    objective: str,
    settings: Settings,
    ledger: RunLedger | None = None,
) -> ScraperResult:
    """Drive perceive -> observe -> assign -> fill -> perceive until the page is done.

    Returns the last `ScraperResult`, which is what the crawl endpoint answers
    with until the Generator exists to produce the three artifacts.
    """
    report: FillReport | None = None
    for _ in range(MAX_ASSIGNMENTS):
        frontier.observe(result.page, report, result.addedControls)

        assignment = frontier.next_assignment()
        if assignment is None:
            return result

        report = fill(tab, assignment, settings, ledger)
        log_contract(log, "FillReport", report)

        result = perceive(
            tab,
            PerceiveRequest(
                job_id=job_id,
                page_index=1,
                objective=objective,
                prior=result.page,
                assignment={assignment.fieldId: assignment.value or ""}
                if assignment.fieldId
                else None,
            ),
            settings,
        )
        log_contract(log, "ScraperResult", result)

    log.error(
        "page did not finish in %d assignments job_id=%s stage_id=%s board=%s",
        MAX_ASSIGNMENTS,
        job_id,
        result.page.stageId,
        frontier.summary(),
    )
    raise RuntimeError(
        f"page {result.page.stageId} did not finish in {MAX_ASSIGNMENTS} assignments"
    )
