"""Carrier secrets at rest: AES-256-GCM, in the same wire format Roadrunner uses.

A stored value is `enc:v1:<iv>:<tag>:<ciphertext>` with each part base64. The
key is 32 bytes, given as 64 hex or 44 base64 characters. Roadrunner's
`src/lib/crypto.js` writes and reads exactly this, so a row encrypted by either
engine decrypts in the other under the same key.

Legacy plaintext (no `enc:v1:` prefix) passes through both ways, so a database
can be migrated row by row. Encrypting with no key configured stores plaintext
and warns once -- a development convenience that production must never rely on.
"""

import base64
import binascii
import logging
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

PREFIX = "enc:v1:"
_IV_BYTES = 12
_TAG_BYTES = 16
_HEX_KEY = re.compile(r"^[0-9a-fA-F]{64}$")

_warned_no_key = False


def parse_key(raw: str | None) -> bytes | None:
    """Turn the configured key string into 32 bytes, or None if unset or malformed.

    Malformed is treated like unset rather than raised: the caller then warns and
    stores plaintext, which is visible, where a crash at import time is not.
    """
    if raw is None or not raw.strip():
        return None
    raw = raw.strip()
    if _HEX_KEY.match(raw):
        return bytes.fromhex(raw)
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    return decoded if len(decoded) == 32 else None


def is_encrypted(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_secret(plain: str | None, key: bytes | None) -> str | None:
    """Encrypt one secret. Empty passes through; already-encrypted is idempotent.

    Without a key the plaintext is returned unchanged and a warning is logged
    once per process.
    """
    global _warned_no_key
    if plain is None or plain == "":
        return plain
    if is_encrypted(plain):
        return plain
    if key is None:
        if not _warned_no_key:
            log.warning(
                "CRED_ENCRYPTION_KEY unset: carrier credentials stored PLAINTEXT (dev only)"
            )
            _warned_no_key = True
        return plain
    iv = os.urandom(_IV_BYTES)
    sealed = AESGCM(key).encrypt(iv, plain.encode("utf-8"), None)
    # The library appends the tag to the ciphertext; the wire format stores them apart.
    data, tag = sealed[:-_TAG_BYTES], sealed[-_TAG_BYTES:]
    parts = (base64.b64encode(b).decode("ascii") for b in (iv, tag, data))
    return PREFIX + ":".join(parts)


def decrypt_secret(stored: str | None, key: bytes | None) -> str | None:
    """Decrypt a stored secret. Plaintext (no prefix) passes through unchanged."""
    if stored is None or not is_encrypted(stored):
        return stored
    if key is None:
        raise RuntimeError("CRED_ENCRYPTION_KEY is required to decrypt carrier credentials")
    try:
        iv_b64, tag_b64, data_b64 = stored[len(PREFIX) :].split(":")
    except ValueError as e:
        raise ValueError("encrypted value is not iv:tag:data") from e
    iv, tag, data = (base64.b64decode(p) for p in (iv_b64, tag_b64, data_b64))
    return AESGCM(key).decrypt(iv, data + tag, None).decode("utf-8")


def redact_secrets(text: str, secrets: list[str | None]) -> str:
    """Replace every occurrence of a known secret in `text` with «redacted».

    Secrets shorter than three characters are skipped: redacting "a" would
    shred the surrounding prose without protecting anything.
    """
    if not text:
        return text
    out = text
    for s in secrets:
        if isinstance(s, str) and len(s) >= 3:
            out = out.replace(s, "«redacted»")
    return out
