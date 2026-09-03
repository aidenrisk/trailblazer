"""HTTP surface. One endpoint, synchronous, no job queue.

The request blocks for the length of the crawl and returns the result. That is
a deliberate choice for now: a queue is worth adding when a crawl takes long
enough that a client cannot hold the connection, and that decision needs a real
carrier page to measure against.
"""

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from trailblazer.contracts.scraper_result import ScraperResult
from trailblazer.loop.login import LoginOutcome, run_login_ensure, run_login_test
from trailblazer.loop.orchestrator import run_crawl
from trailblazer.observability.logging import configure_logging, get_logger
from trailblazer.shared.carrier_creds import UnknownCarrierError, resolve_carrier_creds
from trailblazer.shared.config import get_settings

log = get_logger(__name__)

app = FastAPI(title="Trailblazer", version="0.1.0")


class CrawlRequest(BaseModel):
    """Exactly the payload documented in `scraper_io.txt`.

    No `url`: a crawl starts from the carrier's own portal URL, which is looked
    up from `carrier_id` along with its username and password. The client never
    supplies any of the three.
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
    credentials on file; 503 when the credential store is unreachable; 500 when
    the crawl itself fails, with the underlying message in `detail`.
    """
    configure_logging(get_settings().log_level)

    try:
        creds = resolve_carrier_creds(carrier_id)
    except UnknownCarrierError as e:
        # An unknown carrier is a client-side problem, not a crawl failure, so it
        # is a 400 and never reaches run_crawl.
        raise HTTPException(status_code=400, detail=str(e)) from e
    except psycopg.OperationalError as e:
        raise HTTPException(
            status_code=503, detail=f"credential store unreachable: {e}"
        ) from e

    url = creds.login_url
    try:
        return run_crawl(
            carrier_id=carrier_id,
            url=url,
            insurance_types=request.insuranceTypes,
            business_types=request.businessTypes,
            headed=request.headed,
            creds=creds,
        )
    except Exception as e:
        # Deliberately broad, and deliberately only here: the boundary turns a
        # traceback into a 500 with a message. Nothing below this line swallows
        # anything, so the cause reaches the log intact.
        log.exception("crawl failed carrier_id=%s url=%s", carrier_id, url)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e


class LoginRequest(BaseModel):
    """Options for the two login endpoints."""

    model_config = ConfigDict(populate_by_name=True)

    headed: bool = False
    """Show the browser. Also lets a person type a one-time code when no inbox is configured."""

    fresh: bool = False
    """Ignore the saved session and sign in from nothing (login-ensure only; login-test always does)."""


def _login_call(carrier_id: str, fn, **kwargs) -> LoginOutcome:
    configure_logging(get_settings().log_level)
    try:
        return fn(carrier_id, **kwargs)
    except UnknownCarrierError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except psycopg.OperationalError as e:
        raise HTTPException(status_code=503, detail=f"database unreachable: {e}") from e
    except Exception as e:
        log.exception("%s failed carrier_id=%s", getattr(fn, "__name__", "login"), carrier_id)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e


@app.post("/v0/carriers/{carrier_id}/login-test", response_model=LoginOutcome)
def login_test_endpoint(carrier_id: str, request: LoginRequest) -> LoginOutcome:
    """Do the stored credentials and login prefix still work? Replays in a fresh tab, stops there.

    `status` is `replayed` on success, `needs_authoring` when no prefix is
    stored, `defect` when a recorded step broke (that version is degraded),
    `auth` when the portal refused the credentials, `mfa_timeout` when the code
    never cleared, `browser` on an infrastructure failure. Synchronous, like the
    crawl endpoint; an MFA carrier can take up to the health-check MFA window.
    """
    return _login_call(carrier_id, run_login_test, headed=request.headed)


@app.post("/v0/carriers/{carrier_id}/login-ensure", response_model=LoginOutcome)
def login_ensure_endpoint(carrier_id: str, request: LoginRequest) -> LoginOutcome:
    """Get a tab logged in by the cheapest route and say which one it took.

    `session_held` (the saved session was still valid), `replayed` (the stored
    prefix worked), `needs_authoring` (no prefix, and no FormFiller to capture
    one yet), or a failure kind as for login-test. The session is saved on the
    way out either way.
    """
    return _login_call(carrier_id, run_login_ensure, headed=request.headed, fresh=request.fresh)
