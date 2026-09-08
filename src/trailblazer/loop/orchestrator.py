"""The deterministic Loop that glues the agents together.

A browser already serving CDP on `cdp_port` is attached to rather than replaced,
so one hand-authenticated headed browser serves every agent and every run.

`run_crawl` signs the session in, then runs the perceive -> Frontier -> assign
-> fill -> generate cycle to page completion. Login is a carrier's own class
(`agents/login`), because a portal answers a rejected sign-in with its own form
and only the carrier can say which DOM proves the session took.

Loop also performs backtracking. Frontier answers a `Restart` rather than an
assignment when a page is fully attempted and a gate still owes a side; Loop
renavigates to the flow's entry URL and re-executes the fills already made, in
order, up to the branch point, then takes the owed side. Re-execution goes
through the same `fill()` as any other assignment, so the denylist and every
safety check still apply.

Re-execution is pinned and look-free. Each prefix entry carries the value that
was actually typed, so the filler never re-asks the model and every walk
retraces the same route before branching. And no page is described between
re-executed fills: the boards already hold those pages, the report carries what
a fill changes on a board, and one look after the prefix grades where the
re-execution arrived. On a live run that was 25 looks of 27 spent learning
nothing, at a model call each.
"""

import json
import os
import time
import uuid
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.agents.browser import shared_session
from trailblazer.agents.browser.session import AttachedSession, BrowserSession, devtools_running
from trailblazer.agents.form_filler.form_filler import fill_one, page_problems
from trailblazer.agents.frontier import Frontier
from trailblazer.agents.login import LoginError, resolve_login
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.agents.validator import validate
from trailblazer.agents.vision import read_page, resolve
from trailblazer.agents.generator import Generator
from trailblazer.contracts.assignment import Assignment, FillReport, Restart
from trailblazer.contracts.generation import GenerationRequest
from trailblazer.contracts.page_description import PageDescription
from trailblazer.loop.overlays import OverlayTable, dismiss
from trailblazer.contracts.scraper_result import PerceiveRequest, ScraperResult
from trailblazer.contracts.validation import ValidationRequest
from trailblazer.observability.ledger import RunLedger
from trailblazer.observability.logging import get_logger, log_contract
from trailblazer.shared.config import Settings, get_settings
from trailblazer.shared.dev_carrier_creds import resolve_carrier_creds

log = get_logger(__name__)

LOGIN_SEEDS = {"email": "$EMAIL", "password": "$PASSWORD"}
"""Fields the crawl must not invent a value for.

A login page is an ordinary form, so the filler fills it like any other -- but
typing an invented address gets the run nowhere. The placeholder is resolved
from the carrier's credentials at the moment of typing, so the literal never
enters an assignment, a report or the metadata artifact.
"""

MFA_WAIT_S = 180
"""How long a headed run waits for a one-time code to be entered by hand.

A code is not in the credential store, so the crawl cannot supply it. Where a
human is watching the browser it is cheaper to wait than to fail a login whose
credentials the portal already accepted.
"""

MAX_ASSIGNMENTS = 400
"""Actions allowed on one flow before the crawl is abandoned.

A walk that keeps producing assignments is a defect -- a blocker that never
clears, a control re-added under a new fieldId every perceive -- and looping on
it burns a model call per turn.

Flow-wide, not per page: one crawl walks every route through every page, and a
restart replays the whole route before the branch point, so the count covers
every page of every route plus every replay. `MAX_RESTARTS` per page is what
bounds how many routes there are; this bounds the total work if that fails.
"""


def fill(
    tab: Page,
    assignment: Assignment,
    settings: Settings,
    ledger: RunLedger | None = None,
) -> FillReport:
    """Run one assignment against the live tab."""
    return fill_one(tab, assignment, settings, ledger)


def open_session(settings: Settings, headed: bool = False):
    """Attach to the shared browser, or launch one if none is serving.

    The port comes from the record `trailblazer launch` wrote, so agents share
    one browser without being told where it is. Attaching leaves a login already
    made intact: `AttachedSession.close()` drops the connection only.

    Launching is not a fallback to apologise for -- it is what makes a crawl one
    command. The profile is persistent, so a launched browser carries whatever
    session the last run left, and `_sign_in` establishes one if it does not.
    """
    port = shared_session.live_port(settings.session_file, settings.cdp_port)
    if settings.attach_if_running and devtools_running(port):
        return AttachedSession(cdp_port=port)
    # Nothing is serving, so launch one. This used to raise: the crawl could not
    # log itself in, and a fresh browser perceived a sign-in page and recorded it
    # as the carrier's form. `_sign_in` authenticates now, and the profile
    # persists, so a launched browser is as good as an attached one.
    log.info("no browser on CDP port %d; launching one", port)
    return BrowserSession(
        cdp_port=port,
        headed=headed or settings.headed,
        profile_dir=os.path.expanduser(settings.browser_profile_dir),
    )


def perceive_once(
    url: str,
    page_index: int = 1,
    job_id: str | None = None,
    headed: bool = False,
    settings: Settings | None = None,
) -> ScraperResult:
    """Launch, navigate to `url`, perceive it once, and tear down."""
    settings = settings or get_settings()
    job = job_id or uuid.uuid4().hex[:12]

    with open_session(settings, headed) as session:
        page = session.goto(url)
        result = perceive(page, PerceiveRequest(job_id=job, page_index=page_index), settings)
    log_contract(log, "PageDescription", result.page)
    return result


def run_crawl(
    carrier_id: str,
    url: str,
    insurance_types: list[str],
    business_types: list[str],
    headed: bool = False,
    settings: Settings | None = None,
    seed_values: dict[str, str] | None = None,
    out_dir: Path | None = None,
    validate_script: bool = False,
    start_text: str | None = None,
) -> ScraperResult:
    """Crawl one carrier portal and return the last thing the scraper saw.

    `insurance_types` and `business_types` reach Frontier, which matches them
    against a page's actions to pick which branch of a chooser page to take.
    """
    settings = settings or get_settings()
    if business_types:
        # The value chooser needs the business to make its figures hang together,
        # and it only ever receives `settings`.
        settings = settings.model_copy(update={"crawl_business_type": business_types[0]})
    job_id = uuid.uuid4().hex[:12]
    ledger = RunLedger(job_id=job_id)
    generator = Generator(
        out_dir=Path(out_dir or settings.artifacts_dir) / job_id,
        carrier=carrier_id,
        business_type=business_types[0] if business_types else "",
        insurance_type=insurance_types[0] if insurance_types else "",
        login_url=url,
    )
    frontier = Frontier(
        business_types=business_types,
        insurance_types=insurance_types,
        ledger=ledger,
        seed_values=seed_values or LOGIN_SEEDS,
        start_text=start_text,
    )
    if start_text:
        # The nav step from the landing page into the application is part of the
        # flow, so the replay script has to make it too.
        generator.metadata_doc.config.createSubmissionText = start_text
    log.info(
        "crawl start job_id=%s carrier_id=%s url=%s insurance_types=%s business_types=%s",
        job_id,
        carrier_id,
        url,
        ",".join(insurance_types),
        ",".join(business_types),
    )

    objective = (
        f"Describe this form page. The application is for {', '.join(insurance_types) or 'any'} "
        f"insurance for a {', '.join(business_types) or 'general'} business."
    )

    with open_session(settings, headed) as session:
        tab = session.goto(url)
        _sign_in(tab, carrier_id, settings, ledger, generator, headed)
        result = perceive(
            tab,
            PerceiveRequest(job_id=job_id, page_index=1, objective=objective),
            settings,
            ledger,
        )
        log_contract(log, "ScraperResult", result)
        result = _walk_page(
            tab, result, frontier, job_id, objective, settings, ledger, generator
        )

    # The first route that ran to a terminal page. Its answers are one path the
    # form actually rendered, which the per-field latest across routes is not.
    # Whether that terminal is a quote or an appetite decline is the Validator's
    # determination and it runs after this returns; a settled page is as close
    # as the crawl can get. A crawl that filled nothing has no route to publish.
    if generator.walks:
        chosen = generator.first_settled_walk()
        if chosen is None:
            chosen = generator.walks[-1]
            log.warning(
                "no route reached a terminal page; publishing walk=%d unsettled", chosen
            )
        generator.publish_walk(chosen)
        if validate_script:
            _validate(generator, job_id, carrier_id, settings, headed, ledger)

    if frontier.stuck_reason():
        generator.metadata_doc.stoppedReason = frontier.stuck_reason()
        generator.flush()
    state = generator.state()
    log.info(
        "crawl end job_id=%s stage_id=%s polarity=%s routes=%d flow_done=%s "
        "coverage=%s artifacts=%s",
        job_id,
        result.page.stageId,
        result.polarity,
        frontier.walk,
        frontier.flow_done(),
        frontier.coverage(),
        state.model_dump(),
    )
    ledger.log_summary()
    return result


def _write_replay_config(carrier_id: str, settings: Settings, out_dir: Path) -> Path | None:
    """Write the four-key creds file the replay script reads as `--config`.

    Exactly the keys section 6 fixes. Reading any other name yields an empty
    password that still gets typed, so the portal reports a bad credential
    rather than a missing key.

    The literals are here and nowhere else: the script holds `$EMAIL` and
    `$PASSWORD`, the metadata artifact holds the same placeholders, and this
    file is the only place the real values live. It sits beside the artifacts
    rather than inside them, and is written 0600 because it is a secret on disk.

    `None` when the carrier has no credentials -- a portal may need no login,
    and the fixture does not.
    """
    creds = resolve_carrier_creds(carrier_id, settings)
    if not creds.username or not creds.password:
        log.info("no credentials for carrier_id=%s; the replay runs unauthenticated",
                 carrier_id)
        return None

    path = out_dir / "replay-config.json"
    path.write_text(
        json.dumps(
            {
                "LOGIN_EMAIL": creds.username,
                "LOGIN_PASSWORD": creds.password,
                "MFA_CARRIER_ID": carrier_id,
                "HEADLESS": not settings.headed,
            },
            indent=2,
        )
    )
    path.chmod(0o600)
    log.info("replay config written path=%s", path)
    return path


def _validate(
    generator: Generator,
    job_id: str,
    carrier_id: str,
    settings: Settings,
    headed: bool,
    ledger: RunLedger | None,
) -> None:
    """Run the generated script against the published walk's own answers.

    The published walk is a path the form actually rendered, so replaying it is
    the closest thing to a self-check the crawl can perform: if the script
    cannot reproduce the walk the crawl just made, the script is wrong.

    It exercises one path. The other branches are emitted but not run, and a
    `stuck` outcome here is reported rather than raised -- the artifacts on disk
    are the run's product, and discarding them because the replay failed would
    throw away the evidence needed to fix it.
    """
    answers_path = generator.write_answers()
    config_path = _write_replay_config(carrier_id, settings, generator.out_dir)
    request = ValidationRequest(
        job_id=job_id,
        script_path=str(generator.script_path),
        answers_path=str(answers_path),
        headed=headed,
    )
    try:
        outcome = validate(request, settings, config_path=config_path, ledger=ledger)
    except FileNotFoundError as e:
        log.error("validation could not run job_id=%s: %s", job_id, e)
        return

    log.info(
        "validation job_id=%s outcome=%s reached_quote=%s stopped=%s",
        job_id,
        outcome.outcome,
        outcome.reachedQuote,
        outcome.stoppedReason or "-",
    )
    if not outcome.success:
        log.error(
            "the generated script did not reproduce the crawl's own walk "
            "job_id=%s outcome=%s reason=%s",
            job_id,
            outcome.outcome,
            outcome.stoppedReason or "-",
        )


def _sign_in(
    tab: Page,
    carrier_id: str,
    settings: Settings,
    ledger: RunLedger | None,
    generator: Generator | None = None,
    headed: bool = False,
) -> None:
    """Authenticate the session before the walk begins.

    Raises rather than walking an unauthenticated page: a portal answers a
    rejected sign-in with its own form, which perceives as an ordinary page and
    would be recorded as the carrier's application. An MFA prompt raises too --
    the code is not in the credential store, so the run cannot supply it and a
    human finishes in the shared headed browser.

    A carrier with no registered login class is not an error here. The local
    fixture needs none, and `resolve_carrier_creds` may hold no username at all.
    """
    started = time.monotonic()
    try:
        login = resolve_login(carrier_id, resolve_carrier_creds(carrier_id, settings))
    except KeyError:
        log.info("no login class for carrier_id=%s; continuing unauthenticated", carrier_id)
        return

    result = login.sign_in(tab)
    if result.mfa_required and (headed or settings.headed):
        # The code is not in the credential store, so the run cannot supply it.
        # In a headed run a human is watching the window, so the crawl waits for
        # the sign-in to complete by hand rather than failing a login whose
        # credentials were accepted. A headless run has nobody to ask.
        log.warning(
            "waiting up to %ds for the one-time code to be entered in the browser window",
            MFA_WAIT_S,
        )
        result = login.wait_for_manual_completion(tab, MFA_WAIT_S)
    if ledger is not None:
        ledger.record(
            agent="login",
            action="sign_in",
            detail=carrier_id,
            usd=0.0,
            ok=result.ok,
            ms=int((time.monotonic() - started) * 1000),
        )
    if not result.ok:
        raise LoginError(
            f"could not sign in to {carrier_id}: {result.reason} (at {result.url})"
        )

    # Written before any form page, so login is the script's first stage. The
    # crawl may have skipped the sign-in on a profile that still held a session;
    # the replay runs in a fresh browser and cannot, so the steps are recorded
    # either way.
    if generator is not None:
        generator.record_login(
            [(s.action, s.selector, s.value) for s in result.steps],
            login.authenticated_selector,
        )


def _label_for(page: PageDescription, field_id: str | None) -> str | None:
    """The acting control's label, so the Generator need not re-find it."""
    if field_id is None:
        return None
    return next((c.label for c in page.controls if c.fieldId == field_id), None)


def _walk_page(
    tab,
    result: ScraperResult,
    frontier: Frontier,
    job_id: str,
    objective: str,
    settings: Settings,
    ledger: RunLedger | None = None,
    generator: Generator | None = None,
) -> ScraperResult:
    """Drive perceive -> observe -> assign -> fill -> generate -> perceive until done.

    Returns the last `ScraperResult`, which is what the crawl endpoint answers
    with until the Generator exists to produce the three artifacts.
    """
    entry_url = result.page.url
    prefix: list[tuple[str, Assignment]] = []
    overlays = OverlayTable()
    """How each dialog met on this crawl was cleared. Written to the metadata so
    a restart and the replay script clear it from the record, with no model."""
    """(stageId, assignment) for every action on this route, in order.

    A restart re-executes these from the flow's entry URL to put it back before
    the branch point, crossing page boundaries when the gate is on a page the
    route had already left. Each entry carries its stage so the re-execution
    knows which board a fill belongs to and where to stop.

    Each assignment is stored with the value that was actually typed. Frontier
    leaves a judgment field's value to the filler, and re-executing that
    instruction re-asks the model: on a live run six fields were chosen four
    times over, and nothing pinned the answers, so a later walk could type a
    different business name and no longer retrace the route it was meant to.

    Held here rather than read off disk because the artifacts are keyed by
    questionId and reconciled across captures, so they no longer carry the
    route's order.
    """

    pages: dict[str, PageDescription] = {}
    """stageId -> the description last seen for it.

    What a re-executed fill is filed against in the artifacts: the page was
    described when the route first crossed it, and describing it again costs a
    model call to learn nothing.
    """

    report: FillReport | None = None
    report_stage: str | None = None
    """The stage the report's action ran on. An advance lands the crawl on the
    next page, so without this its report folds into that page's board and
    marks its Next pressed -- every Pie page's forward button is "Next" at the
    same address, so page two arrived already advanced and was declared done
    with nine fields never assigned."""

    seen: dict[str, int] = {}
    """stageId -> screenshots vision has spent on it, capped per page."""

    for _ in range(MAX_ASSIGNMENTS):
        pages[result.page.stageId] = result.page

        # A dialog over the page is cleared before Frontier is shown it. A
        # notice is not a control and not a route step, and leaving it up hides
        # the page's own buttons: Pie's "Locations in Multiple States" modal hid
        # Next, Frontier found no forward control, and a completed page was
        # thrown away for a full route restart.
        result = _clear_overlays(
            tab, result, overlays, job_id, objective, settings, ledger, generator
        )

        frontier.observe(result.page, report, result.addedControls, report_stage)

        decision = frontier.next_assignment()
        if decision is None:
            _record_route_end(generator, frontier, result)
            _record_branch_exploration(generator, frontier)
            return result

        if isinstance(decision, Restart):
            # The route ends here: the restart re-enters the flow and everything
            # after it belongs to the next one.
            _record_route_end(generator, frontier, result)
            result, prefix, report = _restart_walk(
                tab, result, decision, prefix, entry_url, frontier,
                job_id, objective, settings, ledger, generator, pages,
            )
            continue

        assignment = decision
        if assignment.intent == "advance" and assignment.locator == result.page.next:
            # Every field attempted is not every field right. Read the page
            # before moving on; anything wrong is re-filled first.
            problems = page_problems(tab)
            if problems and frontier.reopen([p["locator"] for p in problems], assignment.locator):
                log.info("page not complete before advance: %s", problems)
                # The last fill's report has already been folded in; folding it
                # again would mark the reopened field attempted before it is refilled.
                report = None
                report_stage = None
                continue
        report = fill(tab, assignment, settings, ledger)
        log_contract(log, "FillReport", report)
        if report.ok:
            # Only what succeeded is part of the route. A blocked fill changed
            # nothing on the page, and re-executing it can only block again: on
            # a live run one such fill failed the whole re-execution and cost a
            # real gate its second side.
            prefix.append((result.page.stageId, _pinned(assignment, report)))

        _generate(generator, job_id, result.page, report, frontier.walk, ledger)
        before_stage = result.page.stageId
        report_stage = before_stage
        result = _perceive_after(tab, result, assignment, job_id, objective, settings, ledger)

        if (
            assignment.intent == "advance"
            and result.page.stageId == before_stage
            and assignment.locator == result.page.next
        ):
            # Forward was pressed and the page stayed. That is a rejection to
            # diagnose, not a page to declare done: on a live run the crawl
            # pressed Next six times into a wall and reported the flow finished.
            #
            # Not gated on an unchanged page. A refusal usually *does* change
            # something -- the error text it renders -- so a `-ve` polarity
            # requirement skipped the diagnosis on exactly the pages that
            # needed it, and page three ended its route with eight controls
            # never addressed and Next never re-pressed.

            # A control the page renders but code cannot address is the first
            # thing to settle: it can never be filled, so no amount of
            # re-filling what *is* addressable will satisfy the page. On page
            # three the reopen path spent both its corrections on one claims
            # field while eight lapse controls sat unaddressed beside it.
            if any(not c.locator or not c.unique for c in result.page.controls):
                if _see(tab, result, seen, job_id, settings):
                    report = None
                    report_stage = None
                    continue

            problems = page_problems(tab)
            if problems and frontier.reopen([p["locator"] for p in problems], assignment.locator):
                log.warning("forward press changed nothing; problems=%s", problems)
                report = None
                report_stage = None
                continue
            frontier.mark_stuck(
                f"{assignment.locator!r} pressed on {before_stage} and the page did not "
                "change; no invalid, empty or errored field found"
            )
            _record_route_end(generator, frontier, result)
            _record_branch_exploration(generator, frontier)
            return result

    log.error(
        "flow did not finish in %d actions job_id=%s stage_id=%s walk=%d board=%s",
        MAX_ASSIGNMENTS,
        job_id,
        result.page.stageId,
        frontier.walk,
        frontier.summary(),
    )
    raise RuntimeError(
        f"flow did not finish in {MAX_ASSIGNMENTS} actions; "
        f"stopped on {result.page.stageId} walk {frontier.walk}"
    )


def _pinned(assignment: Assignment, report: FillReport) -> Assignment:
    """The assignment with the value the filler actually typed.

    A value Frontier named -- a gate side, a seed -- is already pinned. A value
    the filler chose exists only on the report, and re-executing the original
    instruction would ask the model again. A report with no value (an advance,
    an expand) leaves the assignment as it was; a blocked fill never reaches
    here, because it is not part of the route.
    """
    if assignment.value is not None or report.valueUsed is None:
        return assignment
    return assignment.model_copy(update={"value": report.valueUsed})


def _generate(
    generator: Generator | None,
    job_id: str,
    page: PageDescription,
    report: FillReport,
    walk: int,
    ledger: RunLedger | None,
) -> None:
    """Append one fill to all three artifacts.

    Appended per fill, not per page: the files on disk are the accumulation, so
    a run that dies mid-page leaves partial work rather than nothing.
    """
    if generator is None:
        return
    generator.append(
        GenerationRequest(
            job_id=job_id,
            carrier=generator.carrier,
            businessType=generator.business_type,
            insuranceType=generator.insurance_type,
            page=page,
            report=report,
            control_label=_label_for(page, report.fieldId),
            walk=walk,
        ),
        ledger,
    )


def _perceive_after(
    tab,
    result: ScraperResult,
    assignment: Assignment,
    job_id: str,
    objective: str,
    settings: Settings,
    ledger: RunLedger | None,
) -> ScraperResult:
    """Look at the page again, telling the scraper what was just done to it."""
    result = perceive(
        tab,
        PerceiveRequest(
            job_id=job_id,
            page_index=1,
            objective=objective,
            prior=result.page,
            assignment={assignment.fieldId: assignment.value or ""}
            if assignment.fieldId
            else None,
        ),
        settings,
        ledger,
    )
    log_contract(log, "ScraperResult", result)
    return result


def _restart_walk(
    tab,
    result: ScraperResult,
    restart: Restart,
    prefix: list[tuple[str, Assignment]],
    entry_url: str,
    frontier: Frontier,
    job_id: str,
    objective: str,
    settings: Settings,
    ledger: RunLedger | None,
    generator: Generator | None,
    pages: dict[str, PageDescription],
) -> tuple[ScraperResult, list[tuple[str, Assignment]], FillReport | None]:
    """Re-enter the flow, re-execute the route up to the gate, take its owed side.

    Returns the page as it stands afterwards, the prefix of the route that has
    just opened, and the last report -- which Loop folds into the board on the
    next turn like any other.

    Navigation goes to the flow's entry URL, not the gate's page: a portal
    carries the application in server-side state, so a form page deep in the
    flow does not render from its URL alone. The re-execution therefore crosses
    page boundaries, including the `advance` actions that move between them,
    which is what lets a gate on page 1 be re-set after page 4 has been seen.

    No page is described between re-executed fills. The values are pinned, the
    pages were described when the route first crossed them, and the report
    carries everything a fill changes on a board. A look after each fill cost a
    model call to re-describe a page the board already held -- 25 of 27 on a
    live run learned nothing. One look after the prefix establishes where the
    re-execution arrived, and that is checked against the gate's stage before
    anything is set.

    Two things this trades away, both bounded to coverage rather than
    corruption. A control that mounts only mid-prefix on a second pass is not
    seen until that page is next described. And a blocker appearing mid-prefix
    is not dismissed; the next fill fails, which abandons the gate below.

    What is re-executed is everything performed before the gate; the gate is
    then set to the side it owes rather than the side it already took. A fill
    coming back `ok: false` aborts the restart -- the page is not in the state
    the route assumed, and continuing would record answers against a page that
    never existed -- so the gate is declared unexplored and the walk resumes
    from wherever the re-execution stopped.
    """
    _record_restart(ledger, restart)
    walk = frontier.open_restart(restart)

    tab.goto(entry_url)
    result = perceive(
        tab,
        PerceiveRequest(job_id=job_id, page_index=1, objective=objective),
        settings,
        ledger,
    )
    log_contract(log, "ScraperResult", result)
    # The re-entered page is what the re-execution starts from; its board is
    # re-established with the controls as they stand now.
    frontier.observe(result.page, None, result.addedControls)
    pages[result.page.stageId] = result.page

    replayed: list[tuple[str, Assignment]] = []
    for stage, assignment in _prefix_before(prefix, restart.fieldId, restart.stageId):
        report = fill(tab, assignment, settings, ledger)
        log_contract(log, "FillReport", report)
        if not report.ok:
            frontier.abandon_gate(
                restart.fieldId,
                f"prefix re-execution failed at {assignment.fieldId or assignment.locator} "
                f"on walk {walk}",
                restart.stageId,
            )
            frontier.fold(stage, report)
            return result, replayed, None

        # The stage is the one recorded when the action first ran -- what an
        # `advance` changes, and what a later restart cuts on. It also names
        # the board the report belongs to, so a fill after an advance lands on
        # the next page's board without a look to tell the two apart.
        replayed.append((stage, assignment))
        frontier.fold(stage, report)
        known = pages.get(stage)
        if known is None:
            raise RuntimeError(
                f"re-executing a fill on stage {stage!r} that was never described"
            )
        _generate(generator, job_id, known, report, walk, ledger)

    # One look, where the answer is consumed: does the page stand where the
    # gate is? The prior is the gate page as last seen, so the diff is against
    # the page this should be rather than the entry page it started from.
    result = perceive(
        tab,
        PerceiveRequest(
            job_id=job_id,
            page_index=1,
            objective=objective,
            prior=pages.get(restart.stageId),
        ),
        settings,
        ledger,
    )
    log_contract(log, "ScraperResult", result)
    frontier.observe(result.page, None, result.addedControls)
    pages[result.page.stageId] = result.page
    _note_divergence(frontier, result, restart.stageId, walk)

    if result.page.stageId != restart.stageId:
        # The replay did not arrive where the gate is, so setting it would act
        # on whatever control happens to carry that fieldId on this page.
        frontier.abandon_gate(
            restart.fieldId,
            f"replay reached {result.page.stageId} not {restart.stageId} on walk {walk}",
            restart.stageId,
        )
        return result, replayed, None

    owed = frontier.open_walk(restart.fieldId, restart.side, restart.stageId)
    report = fill(tab, owed, settings, ledger)
    log_contract(log, "FillReport", report)
    if not report.ok:
        # The side was owed and could not be set: the restart has bought
        # nothing, and asking again would fail the same way until the cap.
        # On a live run three restarts went to a listbox whose "options" were
        # the portal's nav; declared here, the gate stops holding the flow.
        frontier.abandon_gate(
            restart.fieldId,
            f"owed side {restart.side!r} could not be set on walk {walk}: "
            f"{(report.blocked or {}).get('whatYouTried', 'blocked')[:120]}",
            restart.stageId,
        )
        frontier.observe(result.page, report, result.addedControls, restart.stageId)
        return result, replayed, None
    replayed.append((result.page.stageId, owed))

    _generate(generator, job_id, result.page, report, walk, ledger)
    result = _perceive_after(tab, result, owed, job_id, objective, settings, ledger)
    pages[result.page.stageId] = result.page
    return result, replayed, report


def _note_divergence(
    frontier: Frontier, result: ScraperResult, expected_stage: str, walk: int
) -> None:
    """Make a silent mid-prefix change visible in the log.

    Re-execution does not look between fills, so a control that mounted only
    on this pass is not caught there. The one look after the prefix can at
    least report that the page holds a different number of controls than the
    board knew, which is the signal a reader needs to find a missed reveal.
    """
    board = frontier.boards.get(expected_stage)
    if board is None or result.page.stageId != expected_stage:
        return
    seen, known = len(result.page.controls), len(board.controls)
    if seen != known:
        log.warning(
            "re-execution arrived with %d controls on %s but the board knew %d "
            "walk=%d; a control may have mounted only on this pass",
            seen,
            expected_stage,
            known,
            walk,
        )


def _prefix_before(
    prefix: list[tuple[str, Assignment]], field_id: str, stage_id: str
) -> list[tuple[str, Assignment]]:
    """The actions performed before the gate, in order, across pages.

    The gate itself is dropped: the restart exists to take its other side, and
    replaying the side already taken would put the page back on the branch being
    left. Anything after it belongs to the abandoned branch and is not replayed
    -- the owed side renders a different set of fields, and possibly a different
    set of later pages, which Frontier assigns once it sees them.

    The gate is matched on its stage as well as its fieldId, because `fieldId`
    is a per-page counter: `q_001` names a different control on every page, and
    matching on it alone would cut the prefix at the first page.
    """
    cut = next(
        (
            i
            for i, (stage, a) in enumerate(prefix)
            if a.fieldId == field_id and stage == stage_id
        ),
        len(prefix),
    )
    return prefix[:cut]


def _clear_overlays(
    tab,
    result: ScraperResult,
    overlays: OverlayTable,
    job_id: str,
    objective: str,
    settings: Settings,
    ledger: RunLedger | None,
    generator: Generator | None,
) -> ScraperResult:
    """Press the dismisser the scraper named for each notice, then re-describe.

    Only a dialog holding no fillable control is touched: one with fields is
    part of the form and Frontier answers it. How it was cleared is recorded so
    a restart and the replay script clear it from the record, with no model.

    A dialog the model could not classify, or gave no dismisser for, is left
    alone and logged. It reaches Frontier as it stands, which is the honest
    outcome -- guessing at which button keeps the applicant's answers is how a
    "Cancel" gets pressed on a completed page.
    """
    notices = [o for o in result.page.overlays if not o.hasControls]
    if not notices:
        return result

    cleared = 0
    for overlay in notices:
        if overlay.kind != "notice" or not overlay.dismissKey:
            log.error(
                "dialog %r not cleared: kind=%s dismissKey=%r clickables=%s",
                overlay.title, overlay.kind, overlay.dismissKey,
                [c.label for c in overlay.clickables],
            )
            continue
        started = time.monotonic()
        try:
            locator = dismiss(tab, overlay)
        except (RuntimeError, PlaywrightError) as e:
            log.error("could not clear dialog %r: %s", overlay.title, e)
            continue
        overlays.add(overlay.title, locator)
        if generator is not None:
            generator.record_overlay(overlay.title, locator, result.page.stageId)
        if ledger is not None:
            ledger.record(agent="loop", action="dismiss", detail=overlay.title,
                          ms=int((time.monotonic() - started) * 1000))
        cleared += 1

    if not cleared:
        return result
    # The page is a different page with the dialog gone: Next is addressable
    # again, and the controls it covered are visible.
    return perceive(
        tab,
        PerceiveRequest(job_id=job_id, page_index=1, objective=objective, prior=result.page),
        settings,
        ledger,
    )


def _record_route_end(
    generator: Generator | None, frontier: Frontier, result: ScraperResult
) -> None:
    """Note where the route under way stopped, and whether the page had settled.

    A settled page is a terminal the route actually reached; a route that ends
    because a restart re-enters the flow did not. Which terminal it is -- quote
    or appetite decline -- is the Validator's determination.
    """
    if generator is None:
        return
    generator.record_route_end(
        frontier.walk, result.page.stageId, result.polarity == "-ve"
    )


_MAX_LOOKS_PER_STAGE = 2
"""Screenshots one page may cost. A page whose controls vision could not address
twice is not going to yield on the third, and the cap keeps a stuck page from
photographing itself forever."""


def _see(
    tab,
    result: ScraperResult,
    seen: dict[str, int],
    job_id: str,
    settings: Settings,
) -> bool:
    """Look at the page for the addresses code could not measure. True if any was found.

    The last resort, reached only where the deterministic path has nothing left:
    every control was measured, the page's error slots were read, and the page
    still refuses to move without naming a field. What is left is what a person
    would do -- look at it.

    Addresses proven against the badged element are written onto the controls in
    place, so the caller loops round and Frontier assigns them on its next turn
    with no further look. Returns False when nothing was proven, which leaves
    the caller to stop as it would have.
    """
    stage = result.page.stageId
    if seen.get(stage, 0) >= _MAX_LOOKS_PER_STAGE:
        log.warning("vision already looked at %s %d times; not looking again", stage, seen[stage])
        return False
    seen[stage] = seen.get(stage, 0) + 1

    unaddressed = [c for c in result.page.controls if not c.locator or not c.unique]
    if not unaddressed:
        log.info("nothing on %s lacks an address; vision has nothing to add", stage)
        return False

    reading = read_page(
        tab,
        unaddressed,
        "The page refused to move forward and named no field. Which badged elements "
        "does its message refer to, and what words identify each of them?",
        settings,
        job_id,
    )
    addresses = resolve(tab, unaddressed, reading)
    if not addresses:
        return False

    for control in result.page.controls:
        located = addresses.get(control.key)
        if located:
            control.locator = located
            control.unique = True
    log.warning(
        "vision addressed %d control(s) on %s that code could not: %s",
        len(addresses), stage, sorted(addresses.values()),
    )
    return True


def _record_branch_exploration(generator: Generator | None, frontier: Frontier) -> None:
    """Write every page's gate coverage into the metadata artifact.

    The completion assertion reads `branchExploration`, not Frontier's boards,
    so a gate walked both ways or declared unexplored has to reach the artifact
    or it fails the run. Every board is written, not just the page the crawl
    ended on: boards outlive their pages, and an earlier page's gate is the one
    that decided what the later pages rendered.
    """
    if generator is None:
        return
    for board in frontier.coverage():
        walked_both = [
            field_id
            for field_id, gate in board["gates"].items()
            if not gate["remaining"] and len(gate["walked"]) == 2
        ]
        generator.record_branch_exploration(
            board["stageId"], walked_both, board["unexplored"]
        )


def _record_restart(ledger: RunLedger | None, restart: Restart) -> None:
    """One ledger step per restart. Renavigation costs no model call."""
    if ledger is None:
        return
    ledger.record(
        agent="frontier",
        action="restart",
        detail=restart.fieldId,
        usd=0.0,
        ok=True,
    )
