"""Against the REAL backend inbox. Opt-in, read-only, never consumes a code.

    TRAILBLAZER_LIVE=1 TRAILBLAZER_LIVE_CARRIER=thimble uv run pytest tests/live -v

Needs AIDEN_BACKEND_URL, AIDEN_APP_SECRET and AIDEN_INTERNAL_SECRET in the
environment or .env. Uses the backend's peek endpoint, so a code waiting for a
live Roadrunner login is left where it is. A 204 is a pass: the inbox is
reachable and empty, which is its normal state.
"""

import os

import pytest

from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.shared.config import get_settings

pytestmark = pytest.mark.skipif(
    not os.environ.get("TRAILBLAZER_LIVE"),
    reason="set TRAILBLAZER_LIVE=1 (and AIDEN_*) to hit the real backend inbox",
)


def test_the_real_inbox_answers_for_the_configured_carrier() -> None:
    inbox = OtpInbox.from_settings(get_settings())
    assert inbox is not None, "AIDEN_BACKEND_URL / AIDEN_APP_SECRET / AIDEN_INTERNAL_SECRET are not all set"
    slug = os.environ.get("TRAILBLAZER_LIVE_CARRIER", "thimble")

    status, code = inbox.peek(slug)

    assert status in (200, 204), f"backend answered {status}: check the two secrets"
    if status == 200:
        assert code is not None and len(code) == 6
