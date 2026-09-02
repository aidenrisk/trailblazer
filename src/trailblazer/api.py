"""HTTP surface. One endpoint, synchronous, no job queue.

The request blocks for the length of the crawl and returns the result. That is
a deliberate choice for now: a queue is worth adding when a crawl takes long
enough that a client cannot hold the connection, and that decision needs a real
carrier page to measure against.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from trailblazer.contracts.scraper_result import ScraperResult
from trailblazer.loop.orchestrator import run_crawl
from trailblazer.observability.logging import configure_logging, get_logger
from trailblazer.shared.config import get_settings
from trailblazer.shared.dev_carrier_creds import resolve_carrier_creds

log = get_logger(__name__)

app = FastAPI(title="Trailblazer", version="0.1.0")


class CrawlRequest(BaseModel):
    """Exactly the payload documented in `scraper_io.txt`.

    No `url`: a crawl starts from the carrier's own portal URL, which is looked
    up from `carrier_id` along with its username and password. The client never
    supplies any of the three. Today that lookup is the dev stub in
    `dev_carrier_creds.py`, reading `CARRIER_*` from `.env`; swapping it for the
    `carrier_creds` table does not change this model.
    """

    model_config = ConfigDict(populate_by_name=True)

    insuranceTypes: list[str] = []
    businessTypes: list[str] = []
    headed: bool = False


@app.post("/v0/carriers/{carrier_id}/crawl", response_model=ScraperResult)
def crawl(carrier_id: str, request: CrawlRequest) -> ScraperResult:
    """Run the crawl loop against one carrier and return the scraper's result.

    The loop currently holds only the scraper, so this returns one page's
    `ScraperResult`. As frontier and form filler are added the loop grows and
    this handler does not change shape.

    422 comes from FastAPI on a malformed body; 400 when `carrier_id` has no
    credentials on file; 500 when the crawl itself fails, with the underlying
    message in `detail`.
    """
    configure_logging(get_settings().log_level)

    try:
        creds = resolve_carrier_creds(carrier_id)
    except RuntimeError as e:
        # A carrier with no URL is a client-side problem (unknown carrier), not
        # a crawl failure, so it is a 400 and never reaches run_crawl.
        raise HTTPException(status_code=400, detail=str(e)) from e

    url = creds.login_url
    try:
        return run_crawl(
            carrier_id=carrier_id,
            url=url,
            insurance_types=request.insuranceTypes,
            business_types=request.businessTypes,
            headed=request.headed,
        )
    except Exception as e:
        # Deliberately broad, and deliberately only here: the boundary turns a
        # traceback into a 500 with a message. Nothing below this line swallows
        # anything, so the cause reaches the log intact.
        log.exception("crawl failed carrier_id=%s url=%s", carrier_id, url)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
