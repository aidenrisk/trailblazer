"""Where a one-time code comes from when it is not an email.

The shared inbox (`otp_inbox.OtpInbox`) is the normal source: the carrier
emails the code, the backend catches it, we claim it. Two carriers' worth of
exceptions exist, and Roadrunner's `mfa.js` named them first:

- `totp`: the portal enrolled an authenticator app and we hold the seed. The
  code is computed locally (RFC 6238, SHA-1, 30 s, six digits) and no mail is
  involved. Nothing is queued, so nothing is ever stale.
- `manual`: nobody can fetch the code for us, but an operator can. They drop
  it into a file next to the carrier's session jar while the run waits.

All three answer the same `fetch(slug) -> code | None` the MFA wait already
polls, so `wait_for_otp_clear` does not know which it is talking to. `queued`
tells it whether a backlog of stale codes is even possible.
"""

import base64
import hashlib
import hmac
import logging
import struct
import time
from pathlib import Path
from typing import Protocol

from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.shared.carrier_creds import CarrierCreds
from trailblazer.shared.config import Settings

log = logging.getLogger(__name__)


class CodeSource(Protocol):
    queued: bool
    disabled: bool

    def fetch(self, slug: str | None) -> str | None: ...


def totp(secret_base32: str, at: float | None = None, *, digits: int = 6, period: int = 30) -> str:
    """RFC 6238 with the defaults every carrier's enrollment QR uses. Standard library only."""
    clean = "".join(ch for ch in secret_base32.upper() if ch.isalnum())
    key = base32_decode(clean)
    counter = int((at if at is not None else time.time()) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def base32_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 8)
    return base64.b32decode(padded, casefold=True)


class TotpSource:
    """Codes computed from an enrolled authenticator seed. Always has one; never stale."""

    queued = False
    disabled = False

    def __init__(self, secret_base32: str) -> None:
        if not secret_base32 or not secret_base32.strip():
            raise ValueError("a TOTP source needs the enrolled base32 seed")
        self._secret = secret_base32

    def fetch(self, slug: str | None) -> str | None:
        return totp(self._secret)


class FileDropSource:
    """An operator writes the code into a file; the first read consumes it.

    The path is `<sessions_dir>/<slug>.otp` by convention. A stale file from an
    earlier run is ignored by age, because a code older than its own window is
    dead anyway.
    """

    queued = False
    disabled = False

    def __init__(self, path: Path, max_age_s: float = 600.0) -> None:
        self.path = Path(path)
        self.max_age_s = max_age_s
        self._announced = False

    def fetch(self, slug: str | None) -> str | None:
        if not self._announced:
            log.warning("MFA needs a person: write the code for %s to %s", slug, self.path)
            self._announced = True
        try:
            if not self.path.exists():
                return None
            if time.time() - self.path.stat().st_mtime > self.max_age_s:
                return None
            code = self.path.read_text().strip()
            self.path.unlink()
        except OSError:
            return None
        return code or None


def code_source_for(
    creds: CarrierCreds | None, inbox: OtpInbox | None, settings: Settings | None = None
) -> CodeSource | None:
    """The source this carrier's MFA config asks for. None means no source: a person or nothing."""
    if creds is None or not creds.mfa.enabled:
        return inbox
    channel = creds.mfa.channel
    if channel == "totp":
        if not creds.mfa.totp_secret:
            log.error("%s is configured for TOTP but has no seed on file", creds.slug)
            return None
        return TotpSource(creds.mfa.totp_secret)
    if channel == "manual":
        root = Path(settings.sessions_dir) if settings else Path(".sessions")
        return FileDropSource(root / f"{creds.slug}.otp")
    return inbox
