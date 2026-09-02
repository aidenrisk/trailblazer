"""DEV ONLY -- credential lookup stubbed against `.env`. NOT the real integration.

`schema-vertical-carrier.sql` already defines where these belong:

    carrier_creds(carrier_id, login_url, username, password, ...)

Nothing reads that table yet, and login/MFA is getting its own dedicated code
(`flow.md` puts "starting the browser session by login or MFA" inside the crawl
request). This module exists so the crawl endpoint can be developed in isolation
in the meantime: it returns the same `CarrierCreds` shape the real DB-backed
lookup will return, read from `CARRIER_URL` / `CARRIER_USERNAME` /
`CARRIER_PASSWORD`.

**How this gets deleted.** Callers depend only on `resolve_carrier_creds(carrier_id)`
returning a `CarrierCreds`. Move that function to a real module backed by
`carrier_creds`, keep the signature, and delete this file. `CarrierCreds` itself
moves with it -- it is modelled on the table's columns, not on the env vars.

The `carrier_id` argument is accepted and *ignored* here, which is the whole
reason this is a dev stub: one set of env vars cannot describe two carriers. It
is in the signature because the real lookup keys on it.
"""

from pydantic import BaseModel

from trailblazer.observability.logging import get_logger
from trailblazer.shared.config import Settings, get_settings

log = get_logger(__name__)


class CarrierCreds(BaseModel):
    """What a crawl needs to reach a carrier's form. Mirrors `carrier_creds`.

    `username`/`password` are optional because a portal may need no login at all
    (and the local test fixture does not). A caller that requires them must say
    so itself -- this type does not decide that.
    """

    login_url: str
    username: str | None = None
    password: str | None = None


def resolve_carrier_creds(carrier_id: str, settings: Settings | None = None) -> CarrierCreds:
    """Return the credentials for `carrier_id`. DEV: reads `.env`, ignores the id.

    Raises `RuntimeError` when no URL is configured, because a crawl with no URL
    cannot start and the failure is clearer here than three frames deeper in the
    browser session.
    """
    settings = settings or get_settings()

    if not settings.carrier_url:
        raise RuntimeError(
            "no carrier URL configured: set CARRIER_URL in .env. "
            "(dev stub -- carrier_creds.login_url is not wired up yet)"
        )

    log.warning(
        "using DEV credential stub for carrier_id=%s: reading CARRIER_* from .env, "
        "not carrier_creds",
        carrier_id,
    )
    return CarrierCreds(
        login_url=settings.carrier_url,
        username=settings.carrier_username,
        password=settings.carrier_password,
    )
