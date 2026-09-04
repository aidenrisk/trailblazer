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
from trailblazer.contracts.page_description import Action, Control, PageDescription
from trailblazer.observability.ledger import RunLedger
from trailblazer.observability.logging import get_logger, log_contract

log = get_logger(__name__)

# Controls typed as a choice but carrying no options: the listbox is mounted on
# click, so the set cannot be read until the widget is opened (spec §5).
_EXPANDABLE_TYPES = {"select", "other"}

MAX_RESTARTS = 8
"""Restarts allowed on one page before the remaining gates are declared unwalked.

Each one costs a renavigation and a replay of every fill made before the branch
point, so a page carrying many gates would otherwise spend the whole run on it.
Gates past the cap reach `branchExploration.unexplored` with a reason, which the
completion assertion accepts in place of a walk.
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
    ) -> None:
        self.business_types = business_types
        self.insurance_types = insurance_types
        self.ledger = ledger
        self.seed_values = seed_values or {}
        """Values the crawl must not invent, keyed by a label substring.

        Two kinds. A credential is a placeholder the filler resolves at typing
        time, so `$EMAIL` and `$PASSWORD` reach the metadata artifact instead of
        the literal. A class code decides which eligibility questions render at
        all, so a guessed one silently crawls the wrong branch of the form.
        """
        self.board: Board | None = None
        self.page: PageDescription | None = None

    # ----------------------------------------------------------------- observe

    def observe(
        self,
        page: PageDescription,
        report: FillReport | None = None,
        added: list[str] | None = None,
    ) -> None:
        """Fold a new description and the last report into the board.

        `added` is `ScraperResult.addedControls`: the fieldIds new since the
        previous perceive. They are attributed to the assignment the report
        names, which is what makes `revealed` answerable.
        """
        started = time.monotonic()
        if self.board is None or self.board.stage_id != page.stageId:
            if self.board is not None:
                log.info(
                    "board retired stage_id=%s attempted=%d gates_remaining=%d",
                    self.board.stage_id,
                    len(self.board.attempted),
                    len(self.board.half_walked()),
                )
            self.board = Board(stage_id=page.stageId)
            log.info("board opened stage_id=%s url=%s", page.stageId, page.url)

        board = self.board
        self.page = page

        if report is not None:
            self._apply(board, report)

        newly_added = set(added or [])
        revealed_by = report.fieldId if report is not None else None
        for control in page.controls:
            board.add(control, revealed_by if control.fieldId in newly_added else None)

        log_contract(log, "FrontierBoard", board.summary())
        self._record("observe", page.stageId, started)

    def _apply(self, board: Board, report: FillReport) -> None:
        """Mark what the filler did, including a gate side taken or options revealed."""
        if report.fieldId is None:
            # An `advance`: the action was clicked, so it is not re-issued.
            board.advanced.add(report.locator)
            board.dismissed_blockers.add(report.locator)
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

        Priority, highest first: clear a blocker, advance a page with nothing
        fillable, act on an unattempted field, restart the page for a gate still
        owing a side, advance a page whose fields are all done.

        A `Restart` is not an action. Loop performs the renavigation and the
        prefix replay itself and then calls here again; the gate's owed side is
        issued as an ordinary assignment on the walk that opens.
        """
        started = time.monotonic()
        if self.board is None or self.page is None:
            raise RuntimeError("next_assignment called before observe")

        decision = (
            self._dismiss_blocker()
            or self._advance_to_target()
            or self._first_unattempted()
            or self._restart_for_gate()
            or self._advance_when_filled()
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
        """True when every field is attempted and every two-sided gate is walked.

        A pure read: unlike `next_assignment` it records nothing, so Loop can
        ask before deciding whether to assign.
        """
        if self.board is None or self.page is None:
            return False
        return not (
            self._blocking_action()
            or self._target_action()
            or self.board.unattempted()
            or self.board.half_walked()
        )

    def summary(self) -> dict:
        """Board state for logging and for the completion assertion."""
        return self.board.summary() if self.board is not None else {}

    @property
    def walk(self) -> int:
        """The pass over the current page. 1 before any restart."""
        return self.board.walk if self.board is not None else 1

    def open_restart(self, restart: Restart) -> int:
        """Open the walk a `Restart` names, and return its id.

        Called by Loop before it renavigates. The board's attempt record is
        cleared here rather than after the replay, because the replayed fills
        must be recorded against the new walk, not the one being left.
        """
        assert self.board is not None
        walk = self.board.restart()
        if walk != restart.walk:
            raise RuntimeError(
                f"restart names walk {restart.walk} but the board opened {walk}"
            )
        return walk

    # -------------------------------------------------------------- priorities

    def _dismiss_blocker(self) -> Assignment | None:
        """A cookie banner or modal is in the way: click the action that clears it."""
        assert self.board is not None and self.page is not None
        action = self._blocking_action()
        if action is None:
            if self.page.blockers:
                log.warning(
                    "blockers with no dismissing action stage_id=%s blockers=%s",
                    self.board.stage_id,
                    "; ".join(self.page.blockers),
                )
            return None

        self.board.dismissed_blockers.add(action.locator)
        return Assignment(intent="advance", locator=action.locator)

    def _advance_when_filled(self) -> Assignment | None:
        """Press the page's forward control once every field has been acted on.

        `next` is the scraper's measured forward locator, so this covers a form
        page's "Next" and a login page's "Sign In" by the same rule -- a login
        page is a form with two fields and a submit, and needs no special case.
        """
        assert self.board is not None and self.page is not None
        if not self.page.next or self.page.next in self.board.advanced:
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

    def _blocking_action(self) -> Action | None:
        """The first un-clicked action that would clear a blocker, if the page has one.

        The blocker text carries no locator of its own -- `blockers` is a list of
        strings -- so the dismissing element is found among the page's actions by
        its label. An action already clicked is never re-offered, so a blocker
        that does not clear stops the page rather than looping on it.
        """
        assert self.board is not None and self.page is not None
        if not self.page.blockers:
            return None
        words = ("accept", "agree", "dismiss", "close", "got it", "ok", "continue", "allow")
        for action in self.page.actions:
            if action.locator in self.board.dismissed_blockers:
                continue
            if any(w in action.label.casefold() for w in words):
                return action
        return None

    def _advance_to_target(self) -> Assignment | None:
        """Nothing fillable and actions present: click the one matching the crawl."""
        action = self._target_action()
        if action is None:
            return None
        return Assignment(intent="advance", locator=action.locator)

    def _target_action(self) -> Action | None:
        """The un-clicked action to advance on, when the page holds nothing fillable.

        A unique action is preferred over a non-unique one: a portal that repeats
        "Get a Quote" in a header and again in a card offers two locators for one
        destination, and only one of them resolves to a single node.
        """
        assert self.board is not None and self.page is not None
        if self.page.controls or not self.page.actions:
            return None

        candidates = [a for a in self.page.actions if a.locator not in self.board.advanced]
        if not candidates:
            return None

        matched = [a for a in candidates if self._matches_target(a)]
        pool = matched or candidates
        chosen = next((a for a in pool if a.unique), pool[0])
        if not matched:
            log.warning(
                "no action matches the crawl target stage_id=%s types=%s advancing on %r",
                self.board.stage_id,
                ",".join(self.business_types + self.insurance_types),
                chosen.label,
            )
        return chosen

    def _matches_target(self, action: Action) -> bool:
        """Case-insensitive substring of the job's types against label and href.

        Both sides are normalised because the job names a type as an identifier
        (`workers_comp`) and the page renders it as prose ("Workers Comp Quote"):
        matching the raw strings never fires.
        """
        haystack = _normalise(f"{action.label} {action.href}")
        return any(
            _normalise(t) in haystack for t in self.business_types + self.insurance_types
        )

    def _first_unattempted(self) -> Assignment | None:
        """One assignment for the next field with no FillReport.

        A gate is assigned its first side by name rather than left to the
        filler: the board has to know which side was taken to know which one is
        still owed, and a value Frontier did not choose cannot be accounted for.
        """
        assert self.board is not None
        for field_id in self.board.unattempted():
            control = self.board.controls[field_id]
            gate = self.board.gates.get(field_id)
            if gate is not None and gate.remaining:
                value = gate.remaining[0]
                gate.take(value)
                return self._assign(control, value)
            return self._assign(control)
        return None

    def _restart_for_gate(self) -> Restart | None:
        """Ask Loop to reset the page so a gate's owed side can be taken cleanly.

        Setting the gate back is not the same as never having set it: the
        abandoned branch's fields stay mounted and anything filled underneath
        them stays filled (spec §4, "Backtracking"). So the owed side is not
        issued against the page as it stands -- Loop renavigates and replays the
        prefix, and the side is assigned on the walk that opens.

        Past `MAX_RESTARTS` the remaining gates are declared unexplored with a
        reason rather than walked, which the completion assertion accepts.
        """
        assert self.board is not None
        owed = self.board.half_walked()
        if not owed:
            return None

        if self.board.restarts >= MAX_RESTARTS:
            for field_id in owed:
                reason = f"restart cap {MAX_RESTARTS} reached on {self.board.stage_id}"
                self.board.declare_unexplored(field_id, reason)
                log.warning(
                    "gate left unwalked stage_id=%s field_id=%s reason=%s",
                    self.board.stage_id,
                    field_id,
                    reason,
                )
            return None

        field_id = owed[0]
        side = self.board.gates[field_id].remaining[0]
        return Restart(fieldId=field_id, side=side, walk=self.board.walk + 1)

    def open_walk(self, field_id: str, side: str) -> Assignment:
        """Take `side` of gate `field_id`, on the walk a restart has just opened.

        Called by Loop once the renavigation and the prefix replay have put the
        page back before the branch point. The side is marked taken here rather
        than on the report, so a replay that dies after this point still leaves
        the board saying which branch was being attempted.
        """
        assert self.board is not None
        self.board.gates[field_id].take(side)
        return self._assign(self.board.controls[field_id], side)

    def abandon_gate(self, field_id: str, reason: str) -> None:
        """Declare a gate's owed side unwalked, so the page can finish without it.

        Used when the prefix replay did not reproduce the page the walk assumed:
        continuing would record answers against a page that never existed.
        """
        assert self.board is not None
        self.board.declare_unexplored(field_id, reason)
        log.warning(
            "gate left unwalked stage_id=%s field_id=%s reason=%s",
            self.board.stage_id,
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
            if control.type in _EXPANDABLE_TYPES:
                # The choices are not in the DOM until the widget is opened.
                return Assignment(
                    intent="expand", locator=control.locator, fieldId=control.fieldId
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
                )
            return Assignment(
                intent="fill",
                locator=control.locator,
                fieldId=control.fieldId,
                value=self._seed_for(control),
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
        )

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
