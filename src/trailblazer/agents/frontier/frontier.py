"""Frontier: the agent that decides what to act on next.

It holds the board (`board.py`) and nothing else. It never touches the browser,
never chooses a value for a field -- that is the filler's judgment -- and makes
no LLM call: every decision here is a lookup against state it already holds, so
a model would add cost and nondeterminism to arithmetic.

`observe` folds one PageDescription and the previous FillReport into the board;
`next_assignment` reads the board and returns one Assignment, a `Restart`, or
`None` when the page is done. Loop routes on all three: a `Restart` is not an
action and is never handed to the filler -- Loop renavigates, replays the fills
that preceded the branch point, and then asks again.
"""

import re
import time

from trailblazer.agents.frontier.board import CHECKED, Board
from trailblazer.contracts.assignment import Assignment, FillReport, Restart
from trailblazer.contracts.page_description import Action, Control, Option, PageDescription
from trailblazer.observability.ledger import RunLedger
from trailblazer.observability.logging import get_logger, log_contract

log = get_logger(__name__)

# Controls typed as a choice but carrying no options: the listbox is mounted on
# click, so the set cannot be read until the widget is opened (spec §5).
_EXPANDABLE_TYPES = {"select", "other"}

MAX_RESTARTS = 8
"""Restarts allowed on one page before the remaining gates are declared unwalked.

Each one costs a renavigation and a replay of every fill made before the branch
point, so a gate that never leaves its first side would otherwise spend the
whole run on one page. Gates past the cap are declared unexplored with a reason,
which Loop copies to `branchExploration.unexplored` and the completion assertion
accepts in place of a walk.
"""


def _normalise(text: str) -> str:
    """Lowercase, with `_`, `-` and `/` folded to spaces, for substring matching."""
    return re.sub(r"[_\-/]+", " ", text.casefold())


class Frontier:
    """The board for the page currently under the walk, and the next move on it.

    One instance spans a whole crawl. The board inside it is per page: a
    PageDescription carrying a new `stageId` retires the previous board, because
    `fieldId` is a per-page counter and does not identify a control across pages.
    """

    def __init__(
        self,
        business_types: list[str],
        insurance_types: list[str],
        ledger: RunLedger | None = None,
        seed_values: dict[str, str] | None = None,
        start_text: str | None = None,
    ) -> None:
        self.business_types = business_types
        self.insurance_types = insurance_types
        self.ledger = ledger
        self.start_text = start_text or ""
        """The text of the control that starts an application, when the flow
        needs one clicked before any form renders.

        Pie's dashboard is such a page: a search box, filter toggles and a
        paginated table of past submissions, with "Get a Quote" among them.
        Without this the crawl filled the search box and paged the table --
        31 perceives on one page, a model call each -- because no action matched
        the job's types. Reaches the metadata artifact as
        `config.createSubmissionText`.
        """

        self.seed_values = seed_values or {}
        """Values the crawl must not invent, keyed by a label substring.

        Two kinds. A credential is a placeholder the filler resolves at typing
        time, so `$EMAIL` and `$PASSWORD` reach the metadata artifact instead of
        the literal. A class code decides which eligibility questions render at
        all, so a guessed one silently crawls the wrong branch of the form.
        """
        self.board: Board | None = None
        """The board for the stage currently under the walk."""

        self.boards: dict[str, Board] = {}
        """stageId -> its board, kept after the page is left.

        A gate on page 1 decides what page 4 renders, so its owed side is still
        owed once page 4 is reached and the board that records it has to outlive
        its page. `fieldId` is a per-page counter, which is why boards are keyed
        by stage rather than merged.
        """

        self.stage_order: list[str] = []
        """stageIds in the order they were first entered."""

        self.walk_seq: int = 1
        """The route under way. Shared by every board, unlike `Board.walk`.

        One walk is one path from entry to the last page reached, which is what
        makes a walk's answers a set the form actually rendered together.
        """

        self.page: PageDescription | None = None
        self._reached_end = False
        """Whether a walk has run the flow to the page it stops on. See
        `reached_end`: gate coverage waits for it, so the first walk is a
        complete path rather than a branch taken from the middle."""

        self._start_stage: str | None = None
        """The stage the start action was taken on: the landing page.

        The action that starts an application sits in the portal's nav on every
        page. Honoured everywhere, it outranked the fields on the form itself:
        after a restart remounted a nested gate, Frontier clicked "Get a Quote"
        on the form, which reloaded it empty, pressed Next into a wall of
        validation, and restarted for the same gate -- four identical cycles on
        a live run. The start action is a step out of the landing page, taken
        once per route, and chrome anywhere else.

        The landing page's own controls are chrome too. Its gates are never
        walked and never hold the flow open: a filter toggle on a dashboard is
        not a branch of the application.
        """
        self._pin_for: dict[str, str] = {}
        """Ancestor sides to pin when the next restart's walk opens.

        Set by `_restart_for_absent`, consumed by `open_restart`: the pins
        belong to the walk the restart opens, which does not exist yet when the
        Restart is returned.
        """

    # ----------------------------------------------------------------- observe

    def observe(
        self,
        page: PageDescription,
        report: FillReport | None = None,
        added: list[str] | None = None,
        report_stage: str | None = None,
    ) -> None:
        """Fold a new description and the last report into the board.

        `added` is `ScraperResult.addedControls`: the fieldIds new since the
        previous perceive. They are attributed to the assignment the report
        names, which is what makes `revealed` answerable.

        `report_stage` is the stage the report's action ran on. An advance
        lands the crawl on the next page, and its report used to be folded
        into that page's board -- so page two arrived with its own Next already
        recorded as pressed, because every Pie page's forward button is "Next"
        at the same address, and the crawl silently restarted instead of
        leaving. A report belongs to the page it was performed on.
        """
        started = time.monotonic()
        if self.board is None or self.board.stage_id != page.stageId:
            if self.board is not None:
                log.info(
                    "board suspended stage_id=%s attempted=%d gates_remaining=%d",
                    self.board.stage_id,
                    len(self.board.attempted),
                    len(self.board.half_walked()),
                )
            # A board is kept when its page is left, not discarded: a gate on an
            # earlier page still owes a side, and the fields a later page renders
            # depend on which side that is. Re-entering the stage resumes the
            # board rather than starting it over.
            resumed = self.boards.get(page.stageId)
            self.board = resumed or Board(stage_id=page.stageId, walk=self.walk_seq)
            self.boards[page.stageId] = self.board
            if resumed is None:
                self.stage_order.append(page.stageId)
                log.info("board opened stage_id=%s url=%s", page.stageId, page.url)
            else:
                log.info(
                    "board resumed stage_id=%s walk=%d attempted=%d",
                    page.stageId,
                    resumed.walk,
                    len(resumed.attempted),
                )

        board = self.board
        self.page = page

        if report is not None:
            owner = self.boards.get(report_stage) if report_stage else None
            self._apply(owner if owner is not None else board, report)

        newly_added = set(added or [])
        # A reveal is attributed only to an action on this same page.
        same_page = report is not None and (report_stage is None or report_stage == page.stageId)
        revealed_by = report.fieldId if same_page else None
        for control in page.controls:
            board.add(control, revealed_by if control.fieldId in newly_added else None)

        # Presence is replaced, not merged: a control absent from this
        # description is off the page, and assigning against it would credit a
        # gate side that was never taken. `controls` and `gates` keep it, so its
        # walked sides survive until the branch that reveals it is re-entered.
        board.present = {c.fieldId for c in page.controls}

        log_contract(log, "FrontierBoard", board.summary())
        self._record("observe", page.stageId, started)

    def reopen(self, locators: list[str], forward: str | None) -> list[str]:
        """Put fields back on the to-do list after a forward press changed nothing.

        Pressing Next with every field attempted is not the same as every field
        being right. The Loop reads the page for problems; the controls named
        here lose their attempt so they are assigned again, and the forward
        control is forgotten so it can be pressed again once they are. Returns
        the fieldIds reopened.
        """
        assert self.board is not None
        # A problem with no address names nothing; a control with no address
        # cannot be refilled. Reopening either put the page in a loop: Next
        # pressed, the same unaddressable checkbox "reopened", Next pressed --
        # 372 times in one run before the action cap ended it.
        by_locator = {c.locator: f for f, c in self.board.controls.items() if c.locator}
        reopened = [by_locator[l] for l in locators if l and l in by_locator]
        for field_id in reopened:
            self.board.attempted.discard(field_id)
        if forward:
            self.board.advanced.discard(forward)
        if reopened:
            log.warning(
                "reopening stage_id=%s fields=%s after a forward press changed nothing",
                self.board.stage_id, reopened,
            )
        return reopened

    def reopen_with_hint(self, field_id: str, hint: str) -> None:
        """Give a spent field one more attempt, with what the page was seen to say.

        The filler corrected twice against the page's error text and the field
        was still refused; `exhausted` then keeps it from being re-armed by the
        rejection alone. This is the one thing that does re-arm it, because it
        arrives with new information -- the vision fallback's reading of the
        page -- rather than the same rejection again.
        """
        assert self.board is not None
        self.board.hints[field_id] = hint
        self.board.exhausted.discard(field_id)
        self.board.attempted.discard(field_id)
        log.warning(
            "reopening %s on %s with what the page was seen to say: %s",
            field_id, self.board.stage_id, hint[:120],
        )

    def mark_stuck(self, reason: str) -> None:
        """The page is complete as far as anything can tell, and it will not advance.

        Not a completion. The flow is recorded as not done, with the reason, so
        a crawl that pressed Next into a wall reports that rather than success.
        """
        assert self.board is not None
        self.board.stuck = reason
        log.error("stuck stage_id=%s: %s", self.board.stage_id, reason)

    def fold(self, stage_id: str, report: FillReport) -> None:
        """Fold one report into the board for `stage_id`, with no fresh description.

        For prefix re-execution. The pages being re-walked were described on
        the route that first reached them, and `restart_for` keeps every
        control, gate side and reveal across a restart -- so a look between
        re-executed fills is a model call that re-describes a page the board
        already holds. Measured on a live run: 25 of 27 such looks learned
        nothing. The report carries everything the board needs from a fill --
        the attempt, the gate side taken, the advance not to re-press.

        What this does not refresh is `present`. No assignment is chosen against
        a board during re-execution, so a stale `present` is never read; the one
        look after the prefix re-establishes it before the owed side is taken.

        A stage with no board is a defect, not a page to open: every stage in a
        prefix was observed when the route first crossed it.
        """
        started = time.monotonic()
        board = self.boards.get(stage_id)
        if board is None:
            raise RuntimeError(
                f"fold for stage {stage_id!r} but no board exists; "
                f"known stages: {self.stage_order}"
            )
        if self.board is not board:
            log.info("board resumed for fold stage_id=%s walk=%d", stage_id, board.walk)
            self.board = board
        self._apply(board, report)
        self._record("fold", stage_id, started)

    def _apply(self, board: Board, report: FillReport) -> None:
        """Mark what the filler did, including a gate side taken or options revealed."""
        if report.fieldId is None:
            # An `advance` that was clicked is not re-issued. One that was
            # refused was never tried: recording it would leave a dialog with
            # nothing pressable for the rest of the walk.
            if report.ok:
                board.advanced.add(report.locator)
            return

        if report.intent == "expand" and report.ok and report.optionsRevealed:
            # An `expand` reads the choices and commits nothing, so the control
            # is still unanswered: marking it attempted would spend its only
            # turn on the read and leave it with no value and no gate side. The
            # options are recorded before the `board.add` loop in `observe`, so
            # the control picks them up on this pass and `gate_sides` sees the
            # count on the same turn it was read.
            #
            # An expand that failed, or that opened onto nothing, falls through
            # and is marked attempted: the control has no options either way, so
            # re-issuing would expand it forever.
            board.revealed_options[report.fieldId] = [
                Option(label=o["label"], locator=o.get("locator"))
                for o in report.optionsRevealed
            ]
            return

        board.record_fill(report.fieldId)
        gate = board.gates.get(report.fieldId)
        if gate is not None and report.valueUsed is not None:
            gate.take(report.valueUsed)
        elif gate is not None and report.intent == "check":
            # A check with no value recorded still took the checked side.
            gate.take(CHECKED)

        if not report.ok:
            log.warning(
                "assignment failed stage_id=%s field_id=%s intent=%s blocked=%s",
                board.stage_id,
                report.fieldId,
                report.intent,
                report.blocked,
            )

    # --------------------------------------------------------------- decisions

    def next_assignment(self) -> Assignment | Restart | None:
        """The next thing to do, or None when the page is done.

        Priority, highest first: advance a page with nothing fillable, act on
        an unattempted field, advance a page whose fields are all done, restart
        the flow for a gate still owing a side. A dialog over the page is not
        Frontier's to clear: Loop clears it before the page is described here.

        Advancing outranks restarting so a route runs to the end of the flow
        before any branch is revisited. A gate's side decides what the *later*
        pages render, so exhausting one page's gates before moving on would
        record every later page under whichever side the last restart happened
        to leave set, and no walk would be a path from entry to the last page.

        A `Restart` is not an action. Loop performs the re-entry and the prefix
        replay itself and then calls here again; the gate's owed side is issued
        as an ordinary assignment on the walk that opens.
        """
        started = time.monotonic()
        if self.board is None or self.page is None:
            raise RuntimeError("next_assignment called before observe")

        decision = (
            self._advance_to_target()
            or self._first_unattempted()
            or self._advance_when_filled()
            or self._restart_for_gate()
            or self._restart_for_earlier_page()
        )

        if decision is None:
            log.info("page done stage_id=%s", self.board.stage_id)
            self._record("done", self.board.stage_id, started)
            return None

        if isinstance(decision, Restart):
            log_contract(log, "Restart", decision)
            log.info(
                "restart stage_id=%s field_id=%s side=%r walk=%d",
                self.board.stage_id,
                decision.fieldId,
                decision.side,
                decision.walk,
            )
            self._record("restart", decision.fieldId, started)
            return decision

        log_contract(log, "Assignment", decision)
        log.info(
            "assign stage_id=%s intent=%s field_id=%s value=%s",
            self.board.stage_id,
            decision.intent,
            decision.fieldId or "-",
            decision.value or "-",
        )
        self._record("assign", decision.fieldId or decision.intent, started)
        return decision

    def page_done(self) -> bool:
        """True when this page needs nothing more on the route under way.

        Every field attempted and every gate on it either walked every side or
        holding a side for a later route. A gate still owing a side does not
        keep the route on the page: the side decides what the *later* pages
        render, so the route advances and the flow is re-entered for it (spec
        4, "Backtracking"). `flow_done` is what reports the spec's page-done
        condition -- every gate walked on every side -- across the flow.

        A pure read: unlike `next_assignment` it records nothing, so Loop can
        ask before deciding whether to assign.
        """
        if self.board is None or self.page is None:
            return False
        return not (self._target_action() or self.board.unattempted())

    def flow_done(self) -> bool:
        """True when no page owes a gate side that has not been declared.

        The spec's completion condition, read across every page rather than the
        one under the walk: a gate owing a side is a route not yet taken, and a
        gate whose side was declared unexplored carries its reason instead.
        """
        if any(self.boards[s].stuck for s in self.stage_order):
            return False
        return not any(
            self.boards[stage_id].half_walked()
            or self.boards[stage_id].remaining_absent()
            for stage_id in self.stage_order
            if stage_id != self._start_stage
        )

    def stuck_reason(self) -> str | None:
        """Why the flow could not proceed, if a page would not advance."""
        return next((self.boards[s].stuck for s in self.stage_order if self.boards[s].stuck), None)

    def summary(self) -> dict:
        """The current board's state, for logging."""
        return self.board.summary() if self.board is not None else {}

    def coverage(self) -> list[dict]:
        """Every page's gate coverage, in the order the pages were entered.

        Boards outlive their pages, so the crawl ends holding one per stage and
        the last one is not the whole record. The completion assertion reads the
        artifact rather than this, and a gate missing from here never reaches
        the artifact to be graded.
        """
        return [self.boards[stage_id].summary() for stage_id in self.stage_order]

    @property
    def walk(self) -> int:
        """The route under way. 1 before any restart.

        Flow-wide rather than per page: a restart re-enters the flow from its
        entry URL, so every page walked after it belongs to the same route and
        the answers filed under it are a set the form rendered together.
        """
        return self.walk_seq

    def open_restart(self, restart: Restart) -> int:
        """Open the walk a `Restart` names, and return its id.

        Called by Loop before it renavigates. The board's attempt record is
        cleared here rather than after the replay, because the replayed fills
        must be recorded against the new walk, not the one being left.
        """
        assert self.board is not None
        self.walk_seq += 1
        if self.walk_seq != restart.walk:
            raise RuntimeError(
                f"restart names walk {restart.walk} but the flow opened {self.walk_seq}"
            )
        # The replay re-enters from the flow's entry URL, so every page is
        # walked again: each board drops the attempt record it built on the
        # route being left while keeping the gate sides it has taken.
        for board in self.boards.values():
            board.restart_for(self.walk_seq)
        # A nested restart's ancestors are pinned for the walk just opened;
        # `restart_for` cleared the previous walk's pins.
        self.board.pinned.update(self._pin_for)
        self._pin_for = {}
        return self.walk_seq

    # -------------------------------------------------------------- priorities

    def _advance_when_filled(self) -> Assignment | None:
        """Press the page's forward control once every field has been acted on.

        `next` is the scraper's measured forward locator, so this covers a form
        page's "Next" and a login page's "Sign In" by the same rule -- a login
        page is a form with two fields and a submit, and needs no special case.
        """
        assert self.board is not None and self.page is not None
        if not self.page.next:
            # A forward control can appear as the form completes, so a missing
            # one is judged only once every field has been acted on.
            if self.page.blockers or self.board.stuck or self.board.unattempted():
                return None
            if not self.page.controls:
                return None
            # Every field done, nothing blocking, and no way forward. The crawl
            # stops before a form's submit (`_NEXT_PATTERNS` omits it), so this
            # is the flow's end, and the route that reached it is one complete
            # path from entry to terminal. Gate coverage starts from here.
            #
            # Logged loudly because the same shape is also the failure it used
            # to be reported as: if this fires on a middle page the scraper
            # missed that page's Next, and every page after it goes unwalked.
            if not self._reached_end:
                self._reached_end = True
                log.info(
                    "flow end reached stage_id=%s walk=%d fields=%d; "
                    "gate coverage starts now",
                    self.board.stage_id,
                    self.walk_seq,
                    len(self.page.controls),
                )
            return None
        if self.page.next in self.board.advanced:
            return None
        self.board.advanced.add(self.page.next)
        log.info("advancing stage_id=%s locator=%r", self.board.stage_id, self.page.next)
        return Assignment(intent="advance", locator=self.page.next)

    def _seed_for(self, control: Control) -> str | None:
        """A value the caller supplied for this control, matched on its label.

        Matched by label substring because `Control` carries no canonical key --
        that is assigned later, by the Generator -- and `fieldId` is a per-page
        counter that names nothing.
        """
        haystack = f"{control.label} {control.locator}".casefold()
        for needle, value in self.seed_values.items():
            if needle.casefold() in haystack:
                log.info(
                    "seeded field_id=%s label=%r value=%s",
                    control.fieldId,
                    control.label,
                    value if value.startswith("$") else "<supplied>",
                )
                return value
        return None

    def _advance_to_target(self) -> Assignment | None:
        """Nothing fillable and actions present: click the one matching the crawl."""
        action = self._target_action()
        if action is None:
            return None
        return Assignment(intent="advance", locator=action.locator)

    def _target_action(self) -> Action | None:
        """The un-clicked action that starts or continues the crawl's own journey.

        A unique action is preferred over a non-unique one: a portal that repeats
        "Get a Quote" in a header and again in a card offers two locators for one
        destination, and only one of them resolves to a single node.

        An action matching the target is taken even when the page has fillable
        controls. Pie's dashboard is the case: it carries a search box, a row of
        filter toggles and a paginated table of past submissions, none of which
        are the application. Filling first meant the crawl typed into the search
        box, paged the table with its "Next" button and never reached "Get a
        Quote" -- 31 perceives on one page, a model call each.

        Where nothing matches, the page must hold nothing fillable before an
        action is taken: on a form page every unmatched action is chrome, and
        clicking one abandons the form.
        """
        assert self.board is not None and self.page is not None
        if not self.page.actions:
            return None

        candidates = [a for a in self.page.actions if a.locator not in self.board.advanced]
        if not candidates:
            return None

        matched = [a for a in candidates if self._matches_target(a)]
        if matched and self.start_text:
            if self._start_stage is None:
                self._start_stage = self.board.stage_id
            elif self.board.stage_id != self._start_stage:
                # The same nav link on a later page. Taking it leaves the form.
                matched = []
        if matched:
            return next((a for a in matched if a.unique), matched[0])

        # The unmatched fallback is for a landing page with nothing else on it.
        # A page with fields is a form, and a page with a blocker is loading or
        # covered by a dialog -- it has nothing to advance *through*. On Pie's
        # workforce page the fallback clicked the "Business Info" step tab,
        # backwards, against a spinner.
        if self.page.controls or self.page.blockers:
            return None
        chosen = next((a for a in candidates if a.unique), candidates[0])
        log.warning(
            "no action matches the crawl target stage_id=%s types=%s advancing on %r",
            self.board.stage_id,
            ",".join(self.business_types + self.insurance_types),
            chosen.label,
        )
        return chosen

    def _matches_target(self, action: Action) -> bool:
        """True when the action names the job's business or insurance type.

        Both sides are normalised because the job names a type as an identifier
        (`workers_comp`) and the page renders it as prose ("Workers Comp Quote"):
        matching the raw strings never fires.

        `start_text` matches first and exactly. The job's types are our
        vocabulary, not the portal's: Pie links to `/work-comp/business-info`,
        which normalises to "work comp", and the phrase "workers comp" is not a
        substring of it -- so a type match said no and the crawl walked the
        dashboard instead of starting the application. Guessing at stems and
        prefixes to close that gap matches on nothing ("cont" pairs
        `contractors` with "Contact Us"); the portal's own wording is a fact to
        be configured, so `createSubmissionText` carries it.
        """
        haystack = _normalise(f"{action.label} {action.href}")
        if self.start_text:
            return _normalise(self.start_text) in haystack
        return any(
            _normalise(t) in haystack for t in self.business_types + self.insurance_types
        )

    def _first_unattempted(self) -> Assignment | None:
        """One assignment for the next field with no FillReport.

        A gate is assigned its first side by name rather than left to the
        filler: the board has to know which side was taken to know which one is
        still owed, and a value Frontier did not choose cannot be accounted for.

        A control never acted on comes before one reopened on its rejection.
        Board order alone put a reopened field first because it sat higher on
        the page, so a run spent its corrections re-filling a claims box the
        page kept refusing while eight controls the vision fallback had just
        addressed were never touched at all.
        """
        assert self.board is not None
        pending = self.board.unattempted()
        for field_id in sorted(pending, key=lambda f: f in self.board.error_reopens):
            control = self.board.controls[field_id]
            gate = self.board.gates.get(field_id)
            pinned = self.board.pinned.get(field_id)
            if pinned is not None:
                # Holding the branch that mounts a deeper gate. The side is
                # already walked, so `_apply` credits nothing new.
                return self._assign(control, pinned)
            if gate is not None and gate.remaining:
                value = gate.remaining[0]
                gate.take(value)
                return self._assign(control, value)
            return self._assign(control)
        return None

    def _restart_for_gate(self) -> Restart | None:
        """Ask Loop to reset the page so a gate's owed side can be taken cleanly.

        Nothing is restarted before one walk has run the flow to its end. A
        gate's side decides what the *later* pages render, so branching from
        page three leaves pages four and five described under whichever side
        the last restart happened to set, and no walk is a path from entry to
        the terminal. On Pie the crawl branched from workforce-details and
        insurance-history was reached only after three restarts had already
        spent the gate budget.

        Setting the gate back is not the same as never having set it: the
        abandoned branch's fields stay mounted and anything filled underneath
        them stays filled (spec §4, "Backtracking"). So the owed side is not
        issued against the page as it stands -- Loop renavigates and replays the
        prefix, and the side is assigned on the walk that opens.

        Past `MAX_RESTARTS` the remaining gates are declared unexplored with a
        reason rather than walked, which the completion assertion accepts.
        """
        assert self.board is not None
        if self.board.stage_id == self._start_stage:
            return None
        owed = self.board.half_walked()
        absent = self.board.remaining_absent()
        if not owed and not absent:
            return None

        if self.board.restarts >= MAX_RESTARTS:
            for field_id in owed + absent:
                reason = f"restart cap {MAX_RESTARTS} reached on {self.board.stage_id}"
                self.board.declare_unexplored(field_id, reason)
                log.warning(
                    "gate left unwalked stage_id=%s field_id=%s reason=%s",
                    self.board.stage_id,
                    field_id,
                    reason,
                )
            return None

        # A present gate is restarted directly. An absent one owes a side from a
        # page that no longer renders it, so the restart targets the gate that
        # reveals it: replaying up to there and re-taking the revealing side
        # mounts the nested gate again, and its own owed side is assigned once
        # the board sees it.
        if owed:
            if not self.reached_end:
                return None
            # The last owed gate on the page: the deepest choice point on the
            # route is the one to branch from, so the walk that opens differs
            # from the last one as late as possible and everything before it
            # is already known to reach here.
            field_id = owed[-1]
            side = self.board.gates[field_id].remaining[0]
            self.board.charge_restart()
            return Restart(
                fieldId=field_id,
                side=side,
                walk=self.walk_seq + 1,
                stageId=self.board.stage_id,
            )

        return self._restart_for_absent(absent[-1])

    @property
    def reached_end(self) -> bool:
        """Whether any walk has run the flow to the page it stops on.

        Set when a page has every field attempted and no forward control: the
        crawl stops before a form's submit (see `_NEXT_PATTERNS`), so the
        absence of a Next on a completed page is arrival, not a wall. Gate
        coverage begins only once this is true, so the first walk is a path
        from entry to the terminal.
        """
        return self._reached_end

    def _restart_for_earlier_page(self) -> Restart | None:
        """Restart the flow for a gate on a page already left behind.

        An earlier page's gate still owing a side is what makes the next route
        different: the side it was not set to may render a different set of
        later pages, so the flow is re-entered rather than the crawl ending
        here.

        Not before one walk has reached the flow's end. This is reached
        whenever the *current* page yields no assignment, which is not the same
        as the route being over: on Pie's workforce page the assignable fields
        ran out while nine unnamed controls sat unassigned, and the flow was
        re-entered for a business-info gate with two pages never walked. A
        gate's side decides what the later pages render, so branching before
        the end leaves them described under whichever side was set last.

        Pages are taken from the deepest entered backwards, and on each the
        last owed gate first: depth-first, so each new route is the previous
        one with its latest choice changed. An earlier-first order re-entered
        for a page-one gate while later pages still owed sides, and every one
        of those branches waited on a route that might never reach them again.
        """
        assert self.board is not None
        if not self.reached_end:
            return None
        for stage_id in reversed(self.stage_order):
            if stage_id == self.board.stage_id or stage_id == self._start_stage:
                continue
            board = self.boards[stage_id]
            if board.restarts >= MAX_RESTARTS:
                continue
            owed = board.half_walked() or board.remaining_absent()
            if not owed:
                continue

            field_id = owed[-1]
            gate = board.gates[field_id]
            if not gate.remaining:
                continue
            side = gate.remaining[0]
            board.charge_restart()
            log.info(
                "restarting the flow for an earlier page stage_id=%s field_id=%s side=%r",
                stage_id,
                field_id,
                side,
            )
            return Restart(
                fieldId=field_id, side=side, walk=self.walk_seq + 1, stageId=stage_id
            )
        return None

    def _restart_for_absent(self, field_id: str) -> Restart | None:
        """Restart to the branch that mounts `field_id`, so its owed side becomes assignable.

        The nested gate cannot be set while it is off the page. What is set
        instead is the gate that revealed it, back to the side it was on when
        the nested gate appeared -- that side is already walked, so re-taking it
        adds no coverage of its own and exists only to render the child.

        A gate whose revealing side is not known is declared unexplored:
        without a reachable branch point there is no page state in which the
        owed side could be taken.
        """
        assert self.board is not None
        parent, side = self._reachable_ancestor(field_id)
        if parent is None or side is None:
            reason = f"gate is off the page and its revealing branch is unknown on {self.board.stage_id}"
            self.board.declare_unexplored(field_id, reason)
            log.warning(
                "gate left unwalked stage_id=%s field_id=%s reason=%s",
                self.board.stage_id,
                field_id,
                reason,
            )
            return None

        # The declaration above is bookkeeping and always runs; issuing the
        # restart waits until one walk has reached the flow's end, so the gate
        # budget is not spent branching from the middle of the form.
        if not self.reached_end:
            return None

        log.info(
            "restarting to remount a nested gate stage_id=%s field_id=%s parent=%s side=%r",
            self.board.stage_id,
            field_id,
            parent,
            side,
        )
        self._pin_for = self._ancestor_sides(field_id)
        self.board.charge_restart()
        return Restart(
            fieldId=parent,
            side=side,
            walk=self.walk_seq + 1,
            stageId=self.board.stage_id,
        )

    def _ancestor_sides(self, field_id: str) -> dict[str, str]:
        """Every ancestor of `field_id` and the side that keeps it revealed.

        Applied as pins once the restart's walk opens, so the walk that goes
        after a nested gate does not unmount it by taking an intermediate
        gate's owed side on the way down.
        """
        assert self.board is not None
        sides: dict[str, str] = {}
        current = field_id
        while True:
            parent, side = self._revealing_branch(current)
            if parent is None or side is None or parent in sides:
                return sides
            sides[parent] = side
            current = parent

    def _reachable_ancestor(self, field_id: str) -> tuple[str | None, str | None]:
        """The nearest ancestor gate that is on the page, and the side to set it to.

        Nested gates chain: `q_003` may be revealed by `q_002`, which is itself
        revealed by `q_001`. When `q_003` owes a side, every ancestor above it
        can be off the page too, so the chain is walked upward until a gate the
        page currently renders is found -- that is the deepest branch point
        reachable now, and setting it re-mounts the next level down. The owed
        side is assigned once the board sees the gate again, which may take one
        restart per level of nesting.

        A cycle would mean a control revealed by its own descendant. The visited
        set bounds the walk rather than trusting the scraper's links to be
        acyclic.
        """
        assert self.board is not None
        seen: set[str] = set()
        current = field_id
        while True:
            parent, side = self._revealing_branch(current)
            if parent is None or side is None:
                return None, None
            if parent in self.board.present:
                return parent, side
            if parent in seen:
                log.warning(
                    "revealedBy chain cycles stage_id=%s field_id=%s at=%s",
                    self.board.stage_id,
                    field_id,
                    parent,
                )
                return None, None
            seen.add(parent)
            current = parent

    def _revealing_branch(self, field_id: str) -> tuple[str | None, str | None]:
        """The gate and the side of it that mount `field_id`.

        Read from `Control.revealedBy`, which the scraper measures as
        `{fieldId, equals}` -- the parent and the value that brings the child
        onto the page. `Board.revealed` is the fallback: it records which
        assignment a control first appeared after, without the value, in which
        case the parent's first-taken side is the one it appeared under.

        The parent must be a gate, because a restart sets a gate to a named
        side. Presence is not checked here -- `_reachable_ancestor` walks the
        chain upward and decides which link is settable now.
        """
        assert self.board is not None
        control = self.board.controls.get(field_id)
        parent = None
        equals = None
        if control is not None and control.revealedBy is not None:
            parent = control.revealedBy.fieldId
            equals = control.revealedBy.equals
        else:
            parent = self.board.revealed.get(field_id)

        if parent is None:
            return None, None
        gate = self.board.gates.get(parent)
        if gate is None:
            return None, None
        if equals is not None:
            return parent, equals
        return (parent, gate.walked[0]) if gate.walked else (None, None)

    def open_walk(self, field_id: str, side: str, stage_id: str | None = None) -> Assignment:
        """The assignment taking `side` of gate `field_id`, on the walk just opened.

        Called by Loop once the renavigation and the prefix replay have put the
        flow back before the branch point. The side is not marked taken here:
        `_apply` records it from the report's `valueUsed`, so a control that did
        not actually leave the first branch still owes a side rather than being
        credited with one it never took.

        `stage_id` names the board the gate belongs to, which is not the current
        one when the restart targets a page the walk had already left.
        """
        board = self.boards[stage_id] if stage_id is not None else self.board
        assert board is not None
        return self._assign(board.controls[field_id], side)

    def abandon_gate(self, field_id: str, reason: str, stage_id: str | None = None) -> None:
        """Declare a gate's owed side unwalked, so the page can finish without it.

        Used when the prefix replay did not reproduce the page the walk assumed:
        continuing would record answers against a page that never existed.

        `stage_id` names the board the gate belongs to, which is not the current
        one when the restart targeted a page the walk had already left.
        """
        board = self.boards[stage_id] if stage_id is not None else self.board
        assert board is not None
        board.declare_unexplored(field_id, reason)
        log.warning(
            "gate left unwalked stage_id=%s field_id=%s reason=%s",
            board.stage_id,
            field_id,
            reason,
        )

    # ------------------------------------------------------------------ intent

    def _assign(self, control: Control, value: str | None = None) -> Assignment:
        """Build the assignment for `control`, choosing intent from its shape.

        `value` is supplied only when walking a gate to a named side; otherwise
        it is left `None`, because picking a value is the filler's job.
        """
        if control.options is None:
            if control.type in _EXPANDABLE_TYPES and not control.typeahead:
                # The choices are not in the DOM until the widget is opened.
                return Assignment(
                    intent="expand", locator=control.locator, fieldId=control.fieldId
                )
            if control.typeahead:
                # Opening a typeahead reveals nothing; its choices answer what is
                # typed. Filled like a text field, and the filler picks the
                # suggestion that matches.
                return Assignment(
                    intent="fill",
                    locator=control.locator,
                    fieldId=control.fieldId,
                    value=self._seed_for(control),
                    typeahead=True,
                    constraintHint=self._hint_for(control),
                    helpText=control.helpText or None,
                )
            if control.type == "toggle":
                # `value` is "true"/"false": the two sides of a checkbox have no
                # option labels, so the assignment names the state to leave it in
                # rather than saying only "toggle it".
                return Assignment(
                    intent="check",
                    locator=control.locator,
                    fieldId=control.fieldId,
                    value=value,
                    constraintHint=self._hint_for(control),
                )
            return Assignment(
                intent="fill",
                locator=control.locator,
                fieldId=control.fieldId,
                value=self._seed_for(control),
                constraintHint=self._hint_for(control),
                helpText=control.helpText or None,
            )

        option_locator = None
        if value is not None:
            option_locator = next(
                (o.locator for o in control.options if o.label == value and o.locator), None
            )
        return Assignment(
            intent="select",
            locator=control.locator,
            fieldId=control.fieldId,
            value=value,
            optionLocator=option_locator,
            # A value not named here is the filler's to choose, and it needs the
            # choices to choose from: labels to judge by, locators to click.
            options=(
                [{"label": o.label, "locator": o.locator} for o in control.options]
                if value is None else None
            ),
            constraintHint=self._hint_for(control),
            helpText=control.helpText or None,
        )

    def _hint_for(self, control: Control) -> str | None:
        """What the page has said about this field: its stated format, and the
        rejection it is showing now. The filler corrects against both."""
        parts = [control.formatHint]
        if control.error:
            parts.append(f"the page rejected the last answer: {control.error}")
        seen = self.board.hints.get(control.fieldId) if self.board else None
        if seen:
            parts.append(f"the page says this field asks for: {seen}")
        hint = "; ".join(p for p in parts if p)
        return hint or None

    # ------------------------------------------------------------------ ledger

    def _record(self, action: str, detail: str, started: float) -> None:
        """One ledger step. `usd` is always zero: Frontier makes no LLM call."""
        if self.ledger is None:
            return
        self.ledger.record(
            agent="frontier",
            action=action,
            detail=detail,
            usd=0.0,
            ms=int((time.monotonic() - started) * 1000),
            ok=True,
        )
