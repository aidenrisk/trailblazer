"""Client for the shared one-time-code inbox on the Aiden backend.

Carrier portals email their codes to `*@mfa.aidenrisk.com`; a Cloudflare worker
posts them to the backend, which stores each under the carrier's canonical slug
with a ten-minute TTL. This client claims one:

    GET {backend}/api/internal/mfa/{slug}/otp
        x-api-secret:  the backend's APP_SECRET
        x-cron-secret: the backend's CRON_SECRET
    200 {"code": "123456"}   the newest unconsumed code, now consumed
    204                      nothing waiting yet; poll again
    401                      bad secret; auto-pull is off for this run

Roadrunner polls the same endpoint with the same env names, so one `.env` serves
both engines. The code is never logged.
"""

import logging
import re
import time

import httpx

from trailblazer.shared.config import Settings

log = logging.getLogger(__name__)

_SIX_DIGITS = re.compile(r"^\d{6}$")


class OtpInbox:
    """One carrier-agnostic handle on the inbox. `fetch(slug)` claims a code or returns None."""

    def __init__(
        self,
        backend_url: str,
        app_secret: str,
        internal_secret: str,
        *,
        retries: int = 3,
        timeout_s: float = 10.0,
        backoff_s: float = 1.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base = backend_url.rstrip("/")
        self._headers = {"x-api-secret": app_secret, "x-cron-secret": internal_secret}
        self.retries = max(1, retries)
        self.backoff_s = backoff_s
        self._client = client or httpx.Client(timeout=timeout_s)
        self.disabled = False
        """Set once the backend answers 401: the secret is wrong and no poll will succeed."""

    @classmethod
    def from_settings(cls, settings: Settings) -> "OtpInbox | None":
        """The configured inbox, or None when any of the three values is missing.

        None means "no auto-pull": the MFA wait then degrades to a human typing
        the code in a visible browser, or fails fast when there is no browser to see.
        """
        if not (
            settings.aiden_backend_url
            and settings.aiden_app_secret
            and settings.aiden_internal_secret
        ):
            return None
        return cls(
            settings.aiden_backend_url,
            settings.aiden_app_secret,
            settings.aiden_internal_secret,
            retries=settings.mfa_fetch_retries,
            timeout_s=settings.mfa_fetch_timeout_s,
        )

    def fetch(self, slug: str | None) -> str | None:
        """Claim the newest code for `slug`, or None when nothing is waiting.

        Transport failures are retried; a 204 is not. A dropped connection and
        an empty inbox used to look the same, and one blip then burned the whole
        code window.
        """
        if self.disabled or not slug:
            return None
        url = f"{self.base}/api/internal/mfa/{slug}/otp"
        for attempt in range(1, self.retries + 1):
            try:
                response = self._client.get(url, headers=self._headers)
            except httpx.HTTPError as e:
                last = attempt == self.retries
                log.warning(
                    "otp inbox unreachable (%d/%d): %s%s",
                    attempt,
                    self.retries,
                    e,
                    " - giving up this poll" if last else " - retrying",
                )
                if not last:
                    time.sleep(self.backoff_s * attempt)
                continue

            if response.status_code == 200:
                try:
                    code = (response.json() or {}).get("code")
                except ValueError:
                    code = None
                if isinstance(code, str) and _SIX_DIGITS.match(code):
                    log.info("otp inbox returned a code for %s", slug)
                    return code
                log.warning("otp inbox answered 200 without a six-digit code for %s", slug)
                return None
            if response.status_code == 401:
                log.error("otp inbox rejected the internal secret; auto-pull disabled for this run")
                self.disabled = True
                return None
            # 204, or anything else: nothing waiting yet. The caller polls again.
            return None
        return None

    def peek(self, slug: str) -> tuple[int, str | None]:
        """Look without claiming: `(status, code)` from the backend's peek endpoint.

        200 carries the newest waiting code and leaves it in the inbox; 204 means
        the inbox is reachable and empty; 401 means the secret is wrong. This is
        the safe way to prove the plumbing against the real backend, because it
        never consumes a code another run is waiting for.
        """
        url = f"{self.base}/api/internal/mfa/{slug}/peek"
        response = self._client.get(url, headers=self._headers)
        code = None
        if response.status_code == 200:
            try:
                code = (response.json() or {}).get("code")
            except ValueError:
                code = None
        return response.status_code, code if isinstance(code, str) else None

    def reachable(self, slug: str) -> bool:
        """Can this configuration talk to the inbox? True on 200 or 204 from peek."""
        try:
            status, _ = self.peek(slug)
        except httpx.HTTPError as e:
            log.error("otp inbox unreachable: %s", e)
            return False
        if status == 401:
            log.error("otp inbox rejected the internal secret")
        return status in (200, 204)

    def close(self) -> None:
        self._client.close()
