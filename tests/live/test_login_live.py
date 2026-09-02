"""Log into a REAL carrier portal through the chain. Opt-in; costs a one-time code.

    TRAILBLAZER_LIVE_LOGIN=1 TRAILBLAZER_LIVE_CARRIER=thimble uv run pytest tests/live/test_login_live.py -v -s

Needs: the carrier registered in the project database (scripts/upsert_carrier_creds.py),
an LLM key for the Scraper (OPENROUTER_API_KEY or ANTHROPIC_API_KEY), and for an
MFA carrier the three AIDEN_* values. Set HEADED=true to watch, and to let a person
type the code if the inbox does not have it.

Drives Scraper -> Frontier -> a test-only login executor (the FormFiller is not built yet) by hand until the first page that is not
a login stage, then stops: this proves the login, it does not walk the form.
Some identity providers charge one emailed code per run, so do not loop on it.
"""

import os

import pytest

from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.agents.browser.session import BrowserSession
from tests.login_executor import LoginExecutor
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.scraper import Scraper
from trailblazer.contracts import SimpleAssignment, Walk
from trailblazer.shared.carrier_creds import resolve_carrier_creds
from trailblazer.shared.config import get_settings

pytestmark = pytest.mark.skipif(
    not os.environ.get("TRAILBLAZER_LIVE_LOGIN"),
    reason="set TRAILBLAZER_LIVE_LOGIN=1 and TRAILBLAZER_LIVE_CARRIER=<slug> to log into a real carrier",
)

MAX_STEPS = 40


def test_the_chain_logs_into_the_configured_carrier() -> None:
    settings = get_settings()
    slug = os.environ["TRAILBLAZER_LIVE_CARRIER"]
    creds = resolve_carrier_creds(slug, settings)
    inbox = OtpInbox.from_settings(settings)
    if creds.mfa.enabled and inbox is None and not settings.headed:
        pytest.skip(f"{slug} needs a code and neither AIDEN_* nor a headed browser is configured")

    with BrowserSession(cdp_port=settings.cdp_port, headed=settings.headed) as session:
        page = session.goto(creds.login_url)
        scraper = Scraper(page, settings)
        frontier = FrontierAgent()
        filler = LoginExecutor(
            page,
            credentials=creds,
            inbox=inbox,
            human_entry_possible=settings.headed,
            mfa_timeout_s=settings.mfa_timeout_ms / 1000,
        )

        current, diff = scraper.look("live-login")
        assert current.is_login_stage, f"{creds.login_url} did not present a login page: {current.stageId}"

        report = None
        last = None
        for _ in range(MAX_STEPS):
            action = frontier.on_page("live-login", current, diff, report)
            if isinstance(action, Walk):
                break
            if isinstance(action, SimpleAssignment) and action.type == "stop":
                pytest.fail(f"Frontier stopped on {current.stageId}: {action.reason}")
            report = filler.execute("live-login", current.stageId, action)
            assert report.ok, f"{action.type} failed on {current.stageId}: {report.errorClass}"
            last = action
            current, diff = scraper.look("live-login", "post_fill", last, report)
            if not current.is_login_stage:
                break

        assert not current.is_login_stage, f"still on a login stage after {MAX_STEPS} steps: {page.url}"
        print(f"\nlogged into {slug}: {page.url} ({current.stageId})")
        print("login prefix:", [(s.action, s.locator, s.credentialKey) for s in frontier.walk_log])
