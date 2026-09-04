"""The deterministic Loop that glues the agents together.

A browser already serving CDP on `cdp_port` is attached to rather than replaced,
so one hand-authenticated headed browser serves every agent and every run.

`run_crawl` runs the perceive -> Frontier -> assign -> fill -> generate cycle to
page completion.

Loop also performs backtracking. Frontier answers a `Restart` rather than an
assignment when a page is fully attempted and a gate still owes a side; Loop
renavigates to the page's start URL and replays the fills already made, in
order, up to the branch point, then takes the owed side. Replay goes through the
same `fill()` as any other assignment, so the denylist and every safety check
still apply.
"""

import os
import uuid
from pathlib import Path

from playwright.sync_api import Page

from trailblazer.agents.browser import shared_session
from trailblazer.agents.browser.session import AttachedSession, BrowserSession, devtools_running
from trailblazer.agents.form_filler.form_filler import fill_one
from trailblazer.agents.frontier import Frontier
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.agents.generator import Generator
from trailblazer.contracts.assignment import Assignment, FillReport, Restart
from trailblazer.contracts.generation import GenerationRequest
from trailblazer.contracts.page_description import PageDescription
from trailblazer.contracts.scraper_result import PerceiveRequest, ScraperResult
from trailblazer.observability.ledger import RunLedger
from trailblazer.observability.logging import get_logger, log_contract
from trailblazer.shared.config import Settings, get_settings

log = get_logger(__name__)

LOGIN_SEEDS = {"email": "$EMAIL", "password": "$PASSWORD"}
"""Fields the crawl must not invent a value for.

A login page is an ordinary form, so the filler fills it like any other -- but
typing an invented address gets the run nowhere. The placeholder is resolved
from the carrier's credentials at the moment of typing, so the literal never
enters an assignment, a report or the metadata artifact.
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
    """Attach to the shared browser, or launch a private one if none is serving.

    The port comes from the record `trailblazer launch` wrote, so agents share
    one browser without being told where it is. Attaching leaves a hand-done
    login intact: `AttachedSession.close()` drops the connection only.
    """
    port = shared_session.live_port(settings.session_file, settings.cdp_port)
    if settings.attach_if_running:
        if devtools_running(port):
            return AttachedSession(cdp_port=port)
        # Launching here would produce a browser with no login, which perceives
        # a sign-in page instead of the form and reports it as the carrier's.
        raise RuntimeError(
            f"no shared browser serving CDP on port {port}. "
            "Run `trailblazer launch` and log in, then retry. "
            "(Set ATTACH_IF_RUNNING=false to launch a private browser instead.)"
        )
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
) -> ScraperResult:
    """Crawl one carrier portal and return the last thing the scraper saw.

    `insurance_types` and `business_types` reach Frontier, which matches them
    against a page's actions to pick which branch of a chooser page to take.
    """
    settings = settings or get_settings()
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
    )
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

    # The last walk that answered anything. Its answers are a path the form
    # actually rendered; the per-field latest across walks is not. A
    # terminal-aware choice needs the Validator's outcome, which runs after this
    # returns. A crawl that filled nothing has no walk to publish.
    if generator.walks:
        generator.publish_walk(generator.walks[-1])

    state = generator.state()
    log.info(
        "crawl end job_id=%s stage_id=%s polarity=%s board=%s artifacts=%s",
        job_id,
        result.page.stageId,
        result.polarity,
        frontier.summary(),
        state.model_dump(),
    )
    ledger.log_summary()
    return result


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
    """(stageId, assignment) for every action on this route, in order.

    A restart replays these from the flow's entry URL to put it back before the
    branch point, crossing page boundaries when the gate is on a page the route
    had already left. Each entry carries its stage so the replay knows where to
    stop. Held here rather than read off disk because the artifacts are keyed by
    questionId and reconciled across captures, so they no longer carry the
    route's order.
    """

    report: FillReport | None = None
    for _ in range(MAX_ASSIGNMENTS):
        frontier.observe(result.page, report, result.addedControls)

        decision = frontier.next_assignment()
        if decision is None:
            _record_branch_exploration(generator, frontier)
            return result

        if isinstance(decision, Restart):
            result, prefix, report = _restart_walk(
                tab, result, decision, prefix, entry_url, frontier,
                job_id, objective, settings, ledger, generator,
            )
            continue

        assignment = decision
        report = fill(tab, assignment, settings, ledger)
        log_contract(log, "FillReport", report)
        prefix.append((result.page.stageId, assignment))

        _generate(generator, job_id, result.page, report, frontier.walk, ledger)
        result = _perceive_after(tab, result, assignment, job_id, objective, settings, ledger)

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
) -> tuple[ScraperResult, list[tuple[str, Assignment]], FillReport | None]:
    """Re-enter the flow, replay the route up to the gate, and take its owed side.

    Returns the page as it stands afterwards, the prefix of the route that has
    just opened, and the last report -- which Loop folds into the board on the
    next turn like any other.

    Navigation goes to the flow's entry URL, not the gate's page: a portal
    carries the application in server-side state, so a form page deep in the
    flow does not render from its URL alone. The replay therefore crosses page
    boundaries, including the `advance` actions that move between them, which is
    what lets a gate on page 1 be re-set after page 4 has been seen.

    What is replayed is everything performed before the gate; the gate is then
    set to the side it owes rather than the side it already took. A replayed
    fill coming back `ok: false` aborts the restart -- the page is not in the
    state the route assumed, and continuing would record answers against a page
    that never existed -- so the gate is declared unexplored and the walk
    resumes from wherever the replay stopped.
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
    # The re-entered page is what the replay runs against, and the board must
    # hold its controls before anything is assigned against them.
    frontier.observe(result.page, None, result.addedControls)

    replayed: list[tuple[str, Assignment]] = []
    report: FillReport | None = None
    for _, assignment in _prefix_before(prefix, restart.fieldId, restart.stageId):
        report = fill(tab, assignment, settings, ledger)
        log_contract(log, "FillReport", report)
        if not report.ok:
            frontier.abandon_gate(
                restart.fieldId,
                f"prefix replay failed at {assignment.fieldId or assignment.locator} "
                f"on walk {walk}",
                restart.stageId,
            )
            frontier.observe(result.page, report, result.addedControls)
            return result, replayed, None

        # Recorded against the stage the page was on when the action ran, which
        # an `advance` changes: the entry is what a later restart cuts on.
        replayed.append((result.page.stageId, assignment))
        _generate(generator, job_id, result.page, report, walk, ledger)
        result = _perceive_after(tab, result, assignment, job_id, objective, settings, ledger)
        # Folded in per fill, not once at the end: the new walk cleared the
        # attempt record, so a replayed field the board never saw again would be
        # assigned a second time.
        frontier.observe(result.page, report, result.addedControls)
        report = None

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
    replayed.append((result.page.stageId, owed))

    _generate(generator, job_id, result.page, report, walk, ledger)
    result = _perceive_after(tab, result, owed, job_id, objective, settings, ledger)
    return result, replayed, report


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


def _record_branch_exploration(generator: Generator | None, frontier: Frontier) -> None:
    """Write the finished page's gate coverage into the metadata artifact.

    The completion assertion reads `branchExploration`, not Frontier's board, so
    a gate walked both ways or declared unexplored has to reach the artifact
    while the board that knows about it is still open -- the next stage retires
    it.
    """
    if generator is None:
        return
    board = frontier.summary()
    if not board:
        return
    walked_both = [
        field_id
        for field_id, gate in board["gates"].items()
        if not gate["remaining"] and len(gate["walked"]) == 2
    ]
    generator.record_branch_exploration(board["stageId"], walked_both, board["unexplored"])


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
