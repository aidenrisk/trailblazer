"""Carrier credentials, read from `carriers` + `carrier_creds`.

Replaces the dev stub that read `CARRIER_*` from `.env`. The signature it
promised is kept: `resolve_carrier_creds(carrier_id) -> CarrierCreds`. What
changed is that `carrier_id` now means something -- it is the carrier's slug or
its numeric id -- and the password comes back decrypted from its `enc:v1:` form.

Who may call this: FormFiller at capture time and Validator's runner at replay
time, to resolve a `credentialKey`; and Loop, to learn the login URL and whether
the carrier has MFA. Scraper and Frontier never see a `CarrierCreds`.
"""

import logging
from typing import Callable, Literal

from pydantic import BaseModel, Field

from trailblazer.shared.config import Settings, get_settings
from trailblazer.shared.crypto import decrypt_secret, parse_key

log = logging.getLogger(__name__)

RowFetcher = Callable[[str], dict | None]
"""Given a slug or id, return the joined carriers/carrier_creds row, or None."""

_SQL = """
SELECT c.slug, cc.login_url, cc.username, cc.password, cc.mfa
  FROM carriers c
  JOIN carrier_creds cc ON cc.carrier_id = c.id
 WHERE c.slug = %(id)s OR c.id::text = %(id)s
 ORDER BY cc.updated_at DESC
 LIMIT 1
"""


class UnknownCarrierError(LookupError):
    """No carrier, or no credentials on file, for the given slug or id."""


class MfaConfig(BaseModel):
    """How a carrier challenges after the password, from `carrier_creds.mfa`.

    `enabled` is the gate: it turns on the per-carrier login lock and the code
    pull. `channel` says where the code comes from: `email` through the shared
    inbox (the normal case), `totp` computed from an enrolled authenticator seed
    held encrypted in `totp_secret`, or `manual`, an operator dropping the code
    into a file while the run waits. `domains` are the sender domains the
    backend routes email by; configuration for the inbox side, carried here so
    one record describes the whole arrangement.
    """

    enabled: bool = False
    channel: Literal["email", "totp", "manual"] = "email"
    domains: list[str] = Field(default_factory=list)
    totp_secret: str | None = None
    """The enrolled base32 seed, decrypted. Only for `channel == "totp"`."""


class CarrierCreds(BaseModel):
    """What a crawl needs to reach and enter a carrier's portal.

    `username` and `password` are optional on purpose: a portal may need no
    login at all (the local test fixture does not), and an email-OTP-only
    portal has a username and no password. A caller that requires one says so.
    """

    slug: str
    """The carrier's canonical slug. Also the key the OTP inbox routes codes by."""

    login_url: str
    username: str | None = None
    password: str | None = None
    mfa: MfaConfig = Field(default_factory=MfaConfig)

    @property
    def mfa_carrier_id(self) -> str | None:
        """The inbox key when MFA is on, else None (no lock, no inbox pull)."""
        return self.slug if self.mfa.enabled else None

    def secrets(self) -> list[str]:
        """Values that must never reach a log."""
        return [s for s in (self.password, self.mfa.totp_secret) if s]


def _db_fetcher(settings: Settings) -> RowFetcher:
    def fetch(carrier_id: str) -> dict | None:
        from trailblazer.shared.db import fetch_one  # local: psycopg only when a DB is used

        return fetch_one(_SQL, {"id": carrier_id}, settings)

    return fetch


def resolve_carrier_creds(
    carrier_id: str,
    settings: Settings | None = None,
    fetch_row: RowFetcher | None = None,
) -> CarrierCreds:
    """Return the credentials for `carrier_id` (slug or numeric id).

    `fetch_row` is the seam for tests and for callers that already hold a
    connection; the default reads the project database.

    Raises `UnknownCarrierError` when nothing is on file. URL and username are
    trimmed because a stray space in either gets typed into the login form; the
    password is left byte-exact because whitespace can be part of the secret.
    """
    settings = settings or get_settings()
    row = (fetch_row or _db_fetcher(settings))(carrier_id)
    if row is None:
        raise UnknownCarrierError(
            f"no credentials on file for carrier {carrier_id!r}: "
            "add a carriers row with a slug and a carrier_creds row for it"
        )

    key = parse_key(settings.cred_encryption_key)
    password = decrypt_secret(row.get("password"), key)
    mfa_raw = dict(row.get("mfa") or {})
    # The authenticator seed is a secret like the password and is stored the same way.
    if mfa_raw.get("totp_secret"):
        mfa_raw["totp_secret"] = decrypt_secret(mfa_raw["totp_secret"], key)

    creds = CarrierCreds(
        slug=str(row["slug"]),
        login_url=str(row["login_url"]).strip(),
        username=(str(row["username"]).strip() if row.get("username") else None),
        password=password or None,
        mfa=MfaConfig.model_validate(mfa_raw),
    )
    log.info(
        "resolved credentials carrier=%s mfa=%s has_password=%s",
        creds.slug,
        creds.mfa.enabled,
        creds.password is not None,
    )
    return creds
