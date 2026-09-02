"""The deterministic Loop that glues the agents together.

Today the loop has one step: launch a browser, navigate, and run a single
perceive. Frontier, form filler, replay gen and validator slot in around that
call as they are built -- `run_crawl` is the seam, so adding them changes this
module and nothing above it.
"""

import uuid

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.contracts.scraper_result import PerceiveRequest, ScraperResult
from trailblazer.observability.logging import get_logger
from trailblazer.shared.config import Settings, get_settings

log = get_logger(__name__)


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

    with BrowserSession(cdp_port=settings.cdp_port, headed=headed or settings.headed) as session:
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
