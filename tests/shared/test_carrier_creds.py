"""Credential resolution against an injected row fetcher. No database."""

import pytest

from trailblazer.shared.carrier_creds import (
    CarrierCreds,
    MfaConfig,
    UnknownCarrierError,
    resolve_carrier_creds,
)
from trailblazer.shared.config import Settings
from trailblazer.shared.crypto import encrypt_secret, parse_key

KEY_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
SETTINGS = Settings(cred_encryption_key=KEY_HEX, _env_file=None)


def _row(**overrides) -> dict:
    base = {
        "slug": "thimble",
        "login_url": "https://app.thimble.com/login ",
        "username": " agent@aidenrisk.com",
        "password": encrypt_secret("s3cret ", parse_key(KEY_HEX)),
        "mfa": {"enabled": True, "channel": "email", "domains": ["thimble.com"]},
    }
    return {**base, **overrides}


def _fetcher(row: dict | None):
    seen: list[str] = []

    def fetch(carrier_id: str):
        seen.append(carrier_id)
        return row

    fetch.seen = seen  # type: ignore[attr-defined]
    return fetch


def test_decrypts_the_password_and_trims_url_and_username_only() -> None:
    creds = resolve_carrier_creds("thimble", SETTINGS, fetch_row=_fetcher(_row()))

    assert creds.slug == "thimble"
    assert creds.login_url == "https://app.thimble.com/login"
    assert creds.username == "agent@aidenrisk.com"
    assert creds.password == "s3cret "  # byte-exact: the trailing space stays


def test_mfa_config_is_parsed_and_keys_the_inbox_by_slug() -> None:
    creds = resolve_carrier_creds("thimble", SETTINGS, fetch_row=_fetcher(_row()))

    assert creds.mfa == MfaConfig(enabled=True, channel="email", domains=["thimble.com"])
    assert creds.mfa_carrier_id == "thimble"


def test_mfa_off_means_no_inbox_key() -> None:
    creds = resolve_carrier_creds("thimble", SETTINGS, fetch_row=_fetcher(_row(mfa={})))

    assert creds.mfa.enabled is False
    assert creds.mfa_carrier_id is None


def test_otp_only_portal_has_a_username_and_no_password() -> None:
    creds = resolve_carrier_creds("next", SETTINGS, fetch_row=_fetcher(_row(password="")))

    assert creds.username == "agent@aidenrisk.com"
    assert creds.password is None


def test_plaintext_password_passes_through_during_migration() -> None:
    creds = resolve_carrier_creds("thimble", SETTINGS, fetch_row=_fetcher(_row(password="plain")))

    assert creds.password == "plain"


def test_the_lookup_key_is_passed_through_untouched() -> None:
    fetch = _fetcher(_row())
    resolve_carrier_creds("42", SETTINGS, fetch_row=fetch)

    assert fetch.seen == ["42"]


def test_unknown_carrier_raises_a_named_error() -> None:
    with pytest.raises(UnknownCarrierError, match="nobody"):
        resolve_carrier_creds("nobody", SETTINGS, fetch_row=_fetcher(None))


def test_secrets_lists_only_what_must_never_be_logged() -> None:
    creds = CarrierCreds(slug="x", login_url="https://x", username="u", password="p@ss")
    assert creds.secrets() == ["p@ss"]
    assert CarrierCreds(slug="x", login_url="https://x").secrets() == []
