"""Loop's two login responsibilities: open the tab as the carrier, and make sure it is logged in.

Loop is the orchestrator, not an agent, and these are the only two things
login asks of it. Both are sequences of single agent calls and toolkit calls,
so the "agents never call each other" rule holds.

Cheapest route first:

    open the tab with the carrier's saved session, look
      not a login stage        -> session_held      (a warm run logs in zero times)
      stored prefix replays    -> replayed          (candidate promoted to locked)
      prefix broke             -> degrade it, then capture
      no prefix, an executor   -> captured          (published as the next candidate)
      no prefix, no executor   -> needs_authoring   (the FormFiller is not built yet)
      credentials refused      -> auth              (the prefix is kept)
      code never cleared       -> mfa_timeout       (the prefix is kept)

For a carrier whose MFA is on, the per-carrier lock is held from the first
login-stage page until the first page that is not one, so exactly one code is
ever in flight for that carrier.
"""

import contextlib
import logging
import time
from typing import Any, Callable, Iterator, Literal

from playwright.sync_api import Error as PlaywrightError
from pydantic import BaseModel, Field

from trailblazer.agents.browser.login_lock import LoginLock, LoginLockTimeout
from trailblazer.agents.browser.login_replay import replay_login
from trailblazer.agents.browser.net_watch import AuthFailureWatch
from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.browser.session_store import SessionStore, reset_state
from trailblazer.contracts import Diff, FillReport, PageDescription, SimpleAssignment, Walk, WalkStep
from trailblazer.shared.carrier_creds import CarrierCreds, resolve_carrier_creds
from trailblazer.shared.config import Settings, get_settings
from trailblazer.shared.login_programs import LoginProgramStore

log = logging.getLogger(__name__)

Status = Literal[
    "session_held",
    "replayed",
    "captured",
    "needs_authoring",
    "auth",
    "mfa_timeout",
    "defect",
    "browser",
    "blocked",
]


class LoginOutcome(BaseModel):
    """What ensuring a login produced, and what it cost."""

    status: Status
    carrier: str
    reason: str = ""
    final_url: str = ""
    program_version: int | None = None
    """The stored prefix version that was replayed or published."""
    degraded_version: int | None = None
    """A version taken out of service on this run, when a step broke."""
    steps: list[WalkStep] = Field(default_factory=list)
    """The login prefix used or captured. Keys only, never a secret."""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status in ("session_held", "replayed", "captured")


# --------------------------------------------------------------------------- #
# Open the tab as the carrier
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def carrier_tab(
    creds: CarrierCreds,
    settings: Settings | None = None,
    *,
    store: SessionStore | None = None,
    headed: bool = False,
    fresh: bool = False,
    cdp_port: int | None = None,
) -> Iterator[BrowserSession]:
    """A live tab on the carrier's login URL, restored from the saved session unless `fresh`.

    The session is saved again on the way out, whatever happened: a run that
    logged in keeps that login for the next one, and a portal that hands off to a
    second origin acquires that origin's tokens long after login finished.
    """
    settings = settings or get_settings()
    store = store or SessionStore(settings.sessions_dir)
    saved = None if fresh else store.load(creds.slug)
    if saved:
        log.info("restoring the saved session for %s", creds.slug)
    session = BrowserSession(
        cdp_port or settings.cdp_port,
        headed,
        storage_state=saved.state if saved else None,
        session_storage=saved.session_storage if saved else None,
    )
    session.start()
    # Watch the portal's own API from the first request: a restored session that
    # renders the app shell but answers 401 to everything is dead, not held.
    session.auth_failures = AuthFailureWatch().attach(session.context)  # type: ignore[attr-defined]
    try:
        session.goto(creds.login_url)
        yield session
    finally:
        try:
            if session.context is not None:
                store.save(session.context, creds.slug)
        except Exception as e:  # saving is best effort; it must never cost the run
            log.warning("could not save the session for %s: %s", creds.slug, e)
        session.close()


# --------------------------------------------------------------------------- #
# Ensure the tab is logged in
# --------------------------------------------------------------------------- #

CAPTURE_MAX_STEPS = 40


def capture_login(
    job: str,
    page: PageDescription,
    diff: Diff | None,
    *,
    scraper: Any,
    frontier: Any,
    executor: Any,
    max_steps: int = CAPTURE_MAX_STEPS,
) -> tuple[Status, PageDescription, Diff | None, FillReport | None, str]:
    """Drive the chain, one assignment at a time, until the page is no longer a login stage.

    Same protocol as Loop's graph, stopped at the login boundary. The last
    action's FillReport is absorbed into Frontier at the boundary (a code fill
    that advances the page would otherwise be missing from the prefix); the Diff
    of the first form page is returned for the form walk's first call.
    """
    report: FillReport | None = None
    for _ in range(max_steps):
        action = frontier.on_page(job, page, diff, report)
        if isinstance(action, Walk):
            return "auth", page, None, None, "the walk ended while still on the login page"
        if isinstance(action, SimpleAssignment) and action.type == "stop":
            reason = action.reason or "blocked"
            return reason, page, None, None, f"frontier stopped on {page.stageId}: {reason}"
        report = executor.execute(job, page.stageId, action)
        if not report.ok:
            kind = report.errorClass if report.errorClass in ("auth", "mfa_timeout") else "defect"
            return kind, page, None, None, f"{action.type} on {page.stageId} failed: {report.errorClass}"
        page, diff = scraper.look(job, "post_fill", action, report)
        if not page.is_login_stage:
            frontier.absorb(job, report)
            return "captured", page, diff, None, ""
    return "defect", page, None, None, f"still on a login stage after {max_steps} steps"


def ensure_login(
    job: str,
    session: Any,
    creds: CarrierCreds,
    *,
    scraper: Any,
    frontier: Any,
    programs: Any,
    executor: Any = None,
    inbox: OtpInbox | None = None,
    settings: Settings | None = None,
    lock_url: str | None = None,
    human_entry_possible: bool = False,
    on_handoff: Callable[[str], None] | None = None,
    replay: Callable[..., Any] = replay_login,
    lock_factory: Callable[..., Any] = LoginLock,
    save_session: Callable[[], None] | None = None,
) -> tuple[LoginOutcome, PageDescription, Diff | None, FillReport | None]:
    """Get the tab past the login, cheapest route first, and say how.

    Returns the outcome and the first page the form walk should start from,
    with the last action's Diff and FillReport when a capture produced them.
    """
    settings = settings or get_settings()
    started = time.monotonic()

    def done(status: Status, page: PageDescription, reason: str = "", **extra) -> LoginOutcome:
        return LoginOutcome(
            status=status,
            carrier=creds.slug,
            reason=reason,
            final_url=str(session.page.url),
            duration_ms=int((time.monotonic() - started) * 1000),
            **extra,
        )

    def persist() -> None:
        if save_session is not None:
            try:
                save_session()
            except Exception as e:
                log.warning("could not save the session for %s: %s", creds.slug, e)

    page, diff = scraper.look(job)
    if not page.is_login_stage:
        watch = getattr(session, "auth_failures", None)
        if watch is not None and watch.session_looks_dead:
            # The app shell rendered from a stale jar, but the portal's API is refusing it.
            log.warning("[%s] %s: restored session is dead (%s); logging in clean", job, creds.slug, watch.describe())
            try:
                reset_state(session.context, session.page)
                session.page.goto(creds.login_url, wait_until="domcontentloaded")
            except PlaywrightError as e:
                return done("browser", page, f"could not return to the login page: {e}"), page, diff, None
            watch.reset()
            page, diff = scraper.look(job)
        if not page.is_login_stage:
            log.info("[%s] %s: session held, landed on %s", job, creds.slug, page.stageId)
            return done("session_held", page), page, diff, None

    lock = lock_factory(
        creds.mfa_carrier_id,
        lock_url or settings.effective_login_lock_database_url,
        acquire_timeout_s=settings.login_lock_acquire_timeout_s,
        max_hold_s=settings.login_lock_max_hold_s,
    )
    try:
        lock.acquire()
    except LoginLockTimeout as e:
        return done("browser", page, str(e)), page, diff, None

    try:
        degraded_version: int | None = None
        active = programs.active(creds.slug)
        if active is not None:
            log.info("[%s] %s: replaying login prefix v%d", job, creds.slug, active.version)
            out = replay(
                session.page,
                active.steps,
                credentials=creds,
                inbox=inbox,
                login_url=None,
                human_entry_possible=human_entry_possible,
                mfa_timeout_s=settings.mfa_timeout_ms / 1000,
                poll_s=settings.mfa_poll_s,
                settings=settings,
                on_handoff=on_handoff,
            )
            if out.ok:
                if active.status == "candidate":
                    programs.mark_locked(active.id)
                page, diff = scraper.look(job)
                persist()
                return done("replayed", page, out.reason, program_version=active.version, steps=active.steps), page, diff, None
            if out.kind != "defect":
                # Credentials or mailbox, not the recipe: keep it, and do not spend another code on a capture.
                return done(out.kind, page, out.reason, program_version=active.version), page, diff, None
            programs.mark_degraded(active.id, out.reason)
            degraded_version = active.version
            log.warning("[%s] %s: prefix v%d degraded (%s); learning the login again", job, creds.slug, active.version, out.reason)
            # Back to a clean sign-in page before the chain looks at it.
            try:
                reset_state(session.context, session.page)
                session.page.goto(creds.login_url, wait_until="domcontentloaded")
            except PlaywrightError as e:
                return done("browser", page, f"could not return to the login page: {e}", degraded_version=degraded_version), page, diff, None
            page, diff = scraper.look(job)
            if not page.is_login_stage:
                return done("session_held", page, degraded_version=degraded_version), page, diff, None

        if executor is None:
            return (
                done("needs_authoring", page, "no login prefix stored and no FormFiller to capture one", degraded_version=degraded_version),
                page,
                diff,
                None,
            )

        status, page, diff, report, reason = capture_login(
            job, page, diff, scraper=scraper, frontier=frontier, executor=executor
        )
        if status != "captured":
            return done(status, page, reason, degraded_version=degraded_version), page, diff, report
        steps = frontier.login_prefix()
        saved = programs.save(creds.slug, steps) if steps else None
        persist()
        log.info("[%s] %s: login captured, %d steps published as v%s", job, creds.slug, len(steps), saved.version if saved else "-")
        return (
            done(
                "captured",
                page,
                program_version=saved.version if saved else None,
                degraded_version=degraded_version,
                steps=steps,
            ),
            page,
            diff,
            report,
        )
    finally:
        lock.release()


def login_test(
    session: Any,
    creds: CarrierCreds,
    *,
    programs: Any,
    inbox: OtpInbox | None = None,
    settings: Settings | None = None,
    human_entry_possible: bool = False,
    replay: Callable[..., Any] = replay_login,
) -> LoginOutcome:
    """Do the stored credentials and prefix still work? Replay in a fresh tab and stop there.

    Only a broken step degrades the prefix. Uses the shorter health-check MFA
    window: an operator is waiting, not a quote.
    """
    settings = settings or get_settings()
    started = time.monotonic()
    active = programs.active(creds.slug)
    if active is None:
        return LoginOutcome(status="needs_authoring", carrier=creds.slug, reason="no login prefix stored for this carrier")
    out = replay(
        session.page,
        active.steps,
        credentials=creds,
        inbox=inbox,
        login_url=None,
        human_entry_possible=human_entry_possible,
        mfa_timeout_s=settings.login_test_mfa_timeout_ms / 1000,
        poll_s=settings.mfa_poll_s,
    )
    degraded = None
    if out.kind == "defect":
        programs.mark_degraded(active.id, out.reason)
        degraded = active.version
    return LoginOutcome(
        status="replayed" if out.ok else out.kind,
        carrier=creds.slug,
        reason=out.reason,
        final_url=out.final_url,
        program_version=active.version,
        degraded_version=degraded,
        steps=active.steps,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


# --------------------------------------------------------------------------- #
# Entry points the API calls. They build the real dependencies.
# --------------------------------------------------------------------------- #


def run_login_test(carrier_id: str, *, headed: bool = False, settings: Settings | None = None) -> LoginOutcome:
    settings = settings or get_settings()
    creds = resolve_carrier_creds(carrier_id, settings)
    programs = LoginProgramStore(settings)
    inbox = OtpInbox.from_settings(settings)
    # A health check proves the recipe, so it never leans on a saved session.
    with carrier_tab(creds, settings, headed=headed, fresh=True) as session:
        return login_test(
            session, creds, programs=programs, inbox=inbox, settings=settings, human_entry_possible=headed
        )


def run_login_ensure(
    carrier_id: str, *, headed: bool = False, fresh: bool = False, settings: Settings | None = None
) -> LoginOutcome:
    """Reuse the session, replay the prefix, or report that a capture needs an executor."""
    from trailblazer.agents.frontier.frontier import FrontierAgent
    from trailblazer.agents.scraper.scraper import Scraper

    settings = settings or get_settings()
    creds = resolve_carrier_creds(carrier_id, settings)
    programs = LoginProgramStore(settings)
    inbox = OtpInbox.from_settings(settings)
    store = SessionStore(settings.sessions_dir)
    with carrier_tab(creds, settings, store=store, headed=headed, fresh=fresh) as session:
        outcome, _, _, _ = ensure_login(
            f"ensure-{creds.slug}",
            session,
            creds,
            scraper=Scraper(session.page, settings),
            frontier=FrontierAgent(),
            programs=programs,
            executor=None,  # the FormFiller is another team's agent; until it lands, capture is unavailable here
            inbox=inbox,
            settings=settings,
            human_entry_possible=headed,
            save_session=lambda: store.save(session.context, creds.slug),
        )
        return outcome
