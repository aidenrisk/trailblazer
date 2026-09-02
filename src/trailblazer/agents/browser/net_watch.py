"""Is the restored session actually alive? The portal's own API says.

A reused cookie jar that has expired is worse than none: the app shell still
renders, so the first look is not a login stage, and the run skips the login
only to die later on 403s from the portal's API. Counting the portal's own
401 and 403 responses since the tab opened tells the two apart before any
decision is made on the page.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

DEAD_SESSION_THRESHOLD = 3


@dataclass
class AuthFailureWatch:
    """Counts 401/403 responses on a browser context from the moment it is attached."""

    count: int = 0
    samples: list[str] = field(default_factory=list)

    def attach(self, context: Any) -> "AuthFailureWatch":
        context.on("response", self._on_response)
        return self

    def _on_response(self, response: Any) -> None:
        try:
            status = response.status
        except Exception:
            return
        if status in (401, 403):
            self.count += 1
            if len(self.samples) < 5:
                self.samples.append(f"{status} {str(response.url)[:120]}")

    def reset(self) -> None:
        self.count = 0
        self.samples.clear()

    @property
    def session_looks_dead(self) -> bool:
        return self.count >= DEAD_SESSION_THRESHOLD

    def describe(self) -> str:
        return f"{self.count} auth failures from the portal's own API (e.g. {self.samples[0] if self.samples else 'n/a'})"
