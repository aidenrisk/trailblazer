"""Log into a REAL carrier portal. Opt-in; costs a one-time code on some portals.

    TRAILBLAZER_LIVE_LOGIN=1 TRAILBLAZER_LIVE_CARRIER=thimble HEADED=true \
        uv run pytest tests/live/test_login_live.py -v -s

Needs: the carrier registered in the project database (scripts/upsert_carrier_creds.py,
with a slug equal to the backend's canonical carrier id so codes route), an LLM key for the
Scraper, and for an MFA carrier the three AIDEN_* values. HEADED=true lets you watch, and
lets a person type the code if the inbox does not have it.

Two tests, meant to run in order:

1. `test_first_run_captures_and_publishes_the_login` is Loop's real path with a test-only
   executor in the FormFiller's seat (that agent is not built yet): open the tab as the
   carrier, find the login page, learn the login through Scraper -> Frontier -> executor,
   publish the prefix to carrier_login_programs, save the session jar. Stops at the first
   non-login page; it does not walk the form.
2. `test_second_run_reuses_the_session_or_the_stored_prefix` opens a new tab with nothing
   in the FormFiller's seat and expects the saved session to hold or the stored prefix to
   replay. Costs at most one code.

Do not loop on these against a carrier: every attempt on some identity providers is an
emailed code, and repeated failures can trip a lockout.
"""

import os

import pytest

from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.agents.browser.session_store import SessionStore
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.scraper import Scraper
from trailblazer.loop.login import carrier_tab, ensure_login
from trailblazer.shared.carrier_creds import resolve_carrier_creds
from trailblazer.shared.config import get_settings
from trailblazer.shared.login_programs import LoginProgramStore
from tests.login_executor import LoginExecutor

pytestmark = pytest.mark.skipif(
    not os.environ.get("TRAILBLAZER_LIVE_LOGIN"),
    reason="set TRAILBLAZER_LIVE_LOGIN=1 and TRAILBLAZER_LIVE_CARRIER=<slug> to log into a real carrier",
)


def _setup():
    settings = get_settings()
    slug = os.environ["TRAILBLAZER_LIVE_CARRIER"]
    creds = resolve_carrier_creds(slug, settings)
    inbox = OtpInbox.from_settings(settings)
    if creds.mfa.enabled and creds.mfa.channel == "email" and inbox is None and not settings.headed:
        pytest.skip(f"{slug} needs an emailed code and neither AIDEN_* nor HEADED=true is configured")
    return settings, slug, creds, inbox


def _report(slug, outcome, page):
    print(f"\n{slug}: {outcome.status} in {outcome.duration_ms} ms")
    print(f"  reason:  {outcome.reason or '-'}")
    print(f"  parked:  {outcome.final_url} ({page.stageId})")
    print(f"  version: {outcome.program_version}  degraded: {outcome.degraded_version}")
    for i, s in enumerate(outcome.steps, 1):
        print(f"  {i:2d}. {s.action:6s} {s.locator:50s} {s.credentialKey or s.option or ''}")


def test_first_run_captures_and_publishes_the_login() -> None:
    settings, slug, creds, inbox = _setup()
    store = SessionStore(settings.sessions_dir)
    programs = LoginProgramStore(settings)

    with carrier_tab(creds, settings, store=store, headed=settings.headed, fresh=True) as session:
        outcome, page, _, _ = ensure_login(
            f"live-{slug}",
            session,
            creds,
            scraper=Scraper(session.page, settings),
            frontier=FrontierAgent(),
            programs=programs,
            executor=LoginExecutor(
                session.page,
                credentials=creds,
                inbox=inbox,
                human_entry_possible=settings.headed,
                mfa_timeout_s=settings.mfa_timeout_ms / 1000,
                poll_s=settings.mfa_poll_s,
            ),
            inbox=inbox,
            settings=settings,
            human_entry_possible=settings.headed,
            on_handoff=lambda msg: print(f"\nNEEDS A PERSON: {msg}"),
            save_session=lambda: store.save(session.context, creds.slug),
        )
        _report(slug, outcome, page)

    assert outcome.ok, f"{outcome.status}: {outcome.reason}"
    assert not page.is_login_stage
    if outcome.status == "captured":
        assert outcome.program_version is not None and outcome.steps
        assert programs.active(slug) is not None


def test_second_run_reuses_the_session_or_the_stored_prefix() -> None:
    settings, slug, creds, inbox = _setup()
    programs = LoginProgramStore(settings)
    if programs.active(slug) is None:
        pytest.skip(f"no stored login prefix for {slug}; run the first test")
    store = SessionStore(settings.sessions_dir)

    with carrier_tab(creds, settings, store=store, headed=settings.headed) as session:
        outcome, page, _, _ = ensure_login(
            f"live-{slug}-2",
            session,
            creds,
            scraper=Scraper(session.page, settings),
            frontier=FrontierAgent(),
            programs=programs,
            inbox=inbox,
            settings=settings,
            human_entry_possible=settings.headed,
            on_handoff=lambda msg: print(f"\nNEEDS A PERSON: {msg}"),
            save_session=lambda: store.save(session.context, creds.slug),
        )
        _report(slug, outcome, page)

    assert outcome.status in ("session_held", "replayed"), f"{outcome.status}: {outcome.reason}"
    assert not page.is_login_stage
