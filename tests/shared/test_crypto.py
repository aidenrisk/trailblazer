"""Secrets at rest, in Roadrunner's wire format.

The vector below was produced by Roadrunner's `src/lib/crypto.js` under the same
key. If it stops decrypting here, the two engines have drifted apart and rows can
no longer move between them.
"""

import logging

import pytest

from trailblazer.shared.crypto import (
    PREFIX,
    decrypt_secret,
    encrypt_secret,
    is_encrypted,
    parse_key,
    redact_secrets,
)

KEY_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
KEY = parse_key(KEY_HEX)
# encryptSecret('p@ss word ') from roadrunner/src/lib/crypto.js with RR_CRED_ENCRYPTION_KEY=KEY_HEX
ROADRUNNER_CIPHER = "enc:v1:vxEgQYgujctI7Scb:3lB7gVY1BrfmoHbYtbhUCg==:sQyotBpAYAOjIw=="


def test_decrypts_a_value_roadrunner_encrypted() -> None:
    assert decrypt_secret(ROADRUNNER_CIPHER, KEY) == "p@ss word "


def test_round_trip_keeps_the_password_byte_exact() -> None:
    """Trailing whitespace is part of the secret and must survive."""
    plain = "  spaces matter\t"
    assert decrypt_secret(encrypt_secret(plain, KEY), KEY) == plain


def test_ciphertext_is_in_the_shared_wire_format() -> None:
    stored = encrypt_secret("x", KEY)
    assert stored.startswith(PREFIX)
    iv, tag, data = stored[len(PREFIX) :].split(":")
    assert iv and tag and data


def test_encrypting_twice_is_idempotent() -> None:
    once = encrypt_secret("x", KEY)
    assert encrypt_secret(once, KEY) == once


def test_plaintext_passes_through_decrypt_during_migration() -> None:
    assert decrypt_secret("legacy-plaintext", KEY) == "legacy-plaintext"
    assert not is_encrypted("legacy-plaintext")


def test_empty_and_none_pass_through_both_ways() -> None:
    assert encrypt_secret(None, KEY) is None
    assert encrypt_secret("", KEY) == ""
    assert decrypt_secret(None, KEY) is None


def test_no_key_stores_plaintext_and_warns_once(caplog) -> None:
    import trailblazer.shared.crypto as crypto

    crypto._warned_no_key = False
    with caplog.at_level(logging.WARNING):
        assert encrypt_secret("open", None) == "open"
        assert encrypt_secret("open again", None) == "open again"
    assert sum("PLAINTEXT" in r.message for r in caplog.records) == 1


def test_decrypting_without_a_key_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="CRED_ENCRYPTION_KEY"):
        decrypt_secret(ROADRUNNER_CIPHER, None)


def test_wrong_key_fails_rather_than_returning_garbage() -> None:
    other = parse_key("f" * 64)
    with pytest.raises(Exception):
        decrypt_secret(ROADRUNNER_CIPHER, other)


@pytest.mark.parametrize(
    "raw",
    [KEY_HEX, "ASNFZ4mrze8BI0VniavN7wEjRWeJq83vASNFZ4mrze8=", f"  {KEY_HEX}  "],
)
def test_key_accepts_hex_and_base64(raw: str) -> None:
    assert parse_key(raw) == KEY


@pytest.mark.parametrize("raw", [None, "", "short", "zz" * 32, "AAAA"])
def test_malformed_key_reads_as_unset(raw) -> None:
    assert parse_key(raw) is None


def test_redact_replaces_every_secret_and_skips_tiny_ones() -> None:
    text = "login as bob with hunter2, then hunter2 again; a"
    assert redact_secrets(text, ["hunter2", "a", None]) == (
        "login as bob with «redacted», then «redacted» again; a"
    )
