"""The deterministic Loop that glues the agents together.

A browser already serving CDP on `cdp_port` is attached to rather than replaced,
so one hand-authenticated headed browser serves every agent and every run.

Today the loop has one step: launch a browser, navigate, and run a single
perceive. Frontier, form filler, generator and validator slot in around that
call as they are built -- `run_crawl` is the seam, so adding them changes this
module and nothing above it.

"""

import os
import uuid

from trailblazer.agents.browser import shared_session
from trailblazer.agents.browser.session import AttachedSession, BrowserSession, devtools_running
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.contracts.scraper_result import PerceiveRequest, ScraperResult
from trailblazer.observability.logging import get_logger
from trailblazer.shared.config import Settings, get_settings

log = get_logger(__name__)


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
        return perceive(page, PerceiveRequest(job_id=job, page_index=page_index), settings)


def run_crawl(
    carrier_id: str,
    url: str,
    insurance_types: list[str],
    business_types: list[str],
    headed: bool = False,
    settings: Settings | None = None,
) -> ScraperResult:
    """Crawl one carrier portal and return what the scraper saw.

    `insurance_types` and `business_types` are carried for logging and for the
    objective handed to the model; nothing routes on them until Frontier exists
    to walk the branches they select.
    """
    settings = settings or get_settings()
    job_id = uuid.uuid4().hex[:12]
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
        page = session.goto(url)
        result = perceive(
            page,
            PerceiveRequest(job_id=job_id, page_index=1, objective=objective),
            settings,
        )

    log.info(
        "crawl end job_id=%s stage_id=%s polarity=%s",
        job_id,
        result.page.stageId,
        result.polarity,
    )
    return result
