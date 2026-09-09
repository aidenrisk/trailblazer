"""The board: what Frontier knows about one page.

The only state in the pipeline. It answers, for one page, which controls exist,
which have been acted on, which gates still owe a side, which
controls appeared as a result of which assignment, and which walk each fill
belongs to.

A board covers exactly one `stageId`. Advancing to a new stage retires the
previous board rather than extending it, because `fieldId` is a per-page counter
(`page_description.Control.fieldId`) and carries no cross-page identity.
"""

from dataclasses import dataclass, field

from trailblazer.contracts.page_description import Control, Option
from trailblazer.observability.logging import get_logger

log = get_logger(__name__)

MAX_ERROR_REOPENS = 2
"""Corrections a field gets on the page's own rejection before the page stops on it."""

# The two sides of a gate that carries no options: a checkbox is either set or
# not, and there is no option label to name either state.
CHECKED = "true"
UNCHECKED = "false"


def gate_sides(control: Control) -> list[str] | None:
    """Every value `control` must be walked through, or `None` if it is no gate.

    Decided by shape, not by type name (spec §4, "What counts as a gate"): a
    `toggle` is a gate with two sides, and a control carrying two or more
    options is a gate with one side per option. Which option changes the pages
    after it cannot be known without taking it.

    The option clause reads `options` and not `type`, because `type` is the
    model's judgment and `options` is measured. Pie's "Legal Entity Type" is the
    case: perceived as `other`, it is the one gate this pipeline exists to walk,
    and a rule keyed on the type name never saw it. The contract bars
    `text`/`number`/`date` from carrying options at all, so a control holding
    options is choice-bearing whatever it was typed.

    One option is not a gate: there is no second side to owe, so walking it is
    filling it. Each further side costs one re-entry of the flow; `MAX_RESTARTS`
    bounds that per page.
    """
    if control.options is None:
        # `text`/`number`/`date` are barred from options by the contract, so an
        # optionless control here is a toggle: a checkbox with two implicit sides.
        return [CHECKED, UNCHECKED] if control.type == "toggle" else None
    if len(control.options) >= 2:
        return [o.label for o in control.options]
    return None


@dataclass
class GateWalk:
    """One gate's sides and which of them have been taken."""

    walked: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)

    def take(self, value: str) -> None:
        """Move `value` from remaining to walked. A repeat side is not recorded twice."""
        if value in self.remaining:
            self.remaining.remove(value)
        if value not in self.walked:
            self.walked.append(value)


@dataclass
class Board:
    """Every control on one page, and what has been done to each."""

    stage_id: str

    controls: dict[str, Control] = field(default_factory=dict)
    """fieldId -> the control as last perceived."""

    order: list[str] = field(default_factory=list)
    """fieldIds in the order they were first seen, so assignments are stable."""

    attempted: set[str] = field(default_factory=set)
    """fieldIds carrying a FillReport in the current walk.

    Cleared by `restart`: the replay refills the prefix, and the fields after
    the branch point are answered again against whatever the owed side reveals.
    """

    gates: dict[str, GateWalk] = field(default_factory=dict)
    revealed: dict[str, str] = field(default_factory=dict)
    """fieldId -> the fieldId of the assignment that made it appear."""

    advanced: set[str] = field(default_factory=set)
    """Locators of actions already clicked on this page."""

    walk: int = 1
    """Which pass over the page is under way. Incremented by `restart`.

    A gate's second side cannot be taken against the page the first side left
    behind, so Loop renavigates and replays the prefix; the walk id names the
    pass that replay opens, and every fill is filed under it.
    """

    walk_of: dict[str, int] = field(default_factory=dict)
    """fieldId -> the walk its most recent fill was performed in."""

    present: set[str] = field(default_factory=set)
    """fieldIds on the page as last perceived.

    A control revealed by a gate's branch is gone from the page once that gate
    is set to its other side, but stays in `controls` and `gates` so its walked
    sides survive the disappearance. Only presence decides what may be assigned
    now: an assignment against an absent control is performed against nothing
    and its report credits a side that was never taken.
    """

    pinned: dict[str, str] = field(default_factory=dict)
    """fieldId -> the side a gate must hold for the current walk.

    A nested gate is only on the page while its ancestors hold the sides that
    reveal it. Taking an ancestor's owed side would unmount the target before
    it can be reached, so the ancestors are pinned for the walk that goes after
    it and their own owed sides are taken on a later walk.
    """

    restarts: int = 0
    """Restarts issued on this page, counted against `MAX_RESTARTS`."""

    revealed_options: dict[str, list[Option]] = field(default_factory=dict)
    """fieldId -> the choices an `expand` read out of the opened widget.

    Held on the board rather than on the control, because the control is
    replaced from every perceive and a combobox that unmounts its listbox on
    close reports `options: None` again on the look after the `expand`. Without
    this the option count never reaches `gate_sides` and a two-option combobox
    is never recognised as a gate.
    """

    stuck: str | None = None
    """Why this page would not advance, when pressing forward changed nothing
    and the page showed no problem to fix. A stuck page means the flow is not
    done, whatever the gates say."""

    errors: dict[str, str] = field(default_factory=dict)
    """fieldId -> the rejection text the page shows against it right now.

    Measured by the extractor and attributed to the control (its error slot,
    or the nearest field above the message). A control carrying one loses its
    attempt so it is assigned again with the text as its constraint hint: the
    filler is what fixes a field, and it cannot fix what it is never told.
    """

    hints: dict[str, str] = field(default_factory=dict)
    """fieldId -> what the vision fallback read the field as asking for.

    Set once the filler's own corrections are spent: the page's words about the
    field, as a person sees them, handed to the filler as its constraint on one
    further attempt. Kept across walks -- a field's requirement does not change
    with the route."""

    exhausted: set[str] = field(default_factory=set)
    """fieldIds whose correction budget is spent. Reported once, never re-armed:
    the page keeps rendering the same rejection, and each observe would
    otherwise reopen the field again."""

    error_reopens: dict[str, int] = field(default_factory=dict)
    """fieldId -> how many times an error has reopened it this walk. Capped by
    `MAX_ERROR_REOPENS`, past which the error stays and the page stops on it."""

    unexplored: dict[str, str] = field(default_factory=dict)
    """fieldId -> why a gate's owed side was never walked.

    A gate is entered here when the restart cap is reached or when replaying the
    prefix for it failed. Loop copies these into `branchExploration.unexplored`
    when the page finishes, which is what the completion assertion reads.
    """

    def add(self, control: Control, revealed_by: str | None = None) -> bool:
        """Record `control`, returning True when it was not already on the board.

        The control is re-stored on every observe because `options` can change,
        and options an `expand` revealed are merged back in when the fresh
        description carries none: the widget was closed before that perceive, so
        the scraper cannot see them and the gate decision needs the count.
        """
        new = control.fieldId not in self.controls
        if new:
            self.order.append(control.fieldId)
            if revealed_by is not None:
                self.revealed[control.fieldId] = revealed_by
        if control.options is None and control.fieldId in self.revealed_options:
            control = control.model_copy(
                update={"options": self.revealed_options[control.fieldId]}
            )
        self.controls[control.fieldId] = control

        if control.error:
            self.errors[control.fieldId] = control.error
            if control.fieldId in self.attempted and control.locator:
                n = self.error_reopens.get(control.fieldId, 0)
                if n < MAX_ERROR_REOPENS:
                    self.error_reopens[control.fieldId] = n + 1
                    self.attempted.discard(control.fieldId)
                    log.warning(
                        "reopening %s (%r) on its rejection: %s",
                        control.fieldId, control.label[:40], control.error[:120],
                    )
                elif control.fieldId not in self.exhausted:
                    # Spent, and reported once. Every later observe sees the
                    # same rejection, and re-arming on each cost one live run
                    # three more refills of a field the page had already
                    # refused nine times -- while eight controls vision had
                    # just addressed sat unfilled beside it.
                    self.exhausted.add(control.fieldId)
                    log.error(
                        "%s (%r) still rejected after %d corrections; not retried: %s",
                        control.fieldId, control.label[:40], n, control.error[:120],
                    )
        else:
            self.errors.pop(control.fieldId, None)

        unsettable = control.disabled or control.additionalRow or not control.locator
        if not control.locator and new:
            # No id, name, test-id or label the extractor could turn into an
            # address. Playwright refuses an empty selector, and Frontier tried
            # four such checkboxes on Pie's insurance-history page -- then would
            # have restarted for each one's other side. Never assigned, never a
            # gate; the gap is logged so it reaches the reader, not swallowed.
            log.warning("control %s (%r) has no locator and cannot be set", control.fieldId, control.label[:40])
        sides = None if unsettable else gate_sides(control)
        if sides is None:
            self.gates.pop(control.fieldId, None)
        elif control.fieldId not in self.gates:
            self.gates[control.fieldId] = GateWalk(remaining=list(sides))
        return new

    def unattempted(self) -> list[str]:
        """fieldIds with no FillReport yet, in the order they were seen.

        A disabled control is never one: the page will not let it be set, so an
        assignment against it spends the filler's click timeout and returns
        blocked. Pie's "Agency / Program" is the case -- pre-filled from the
        logged-in agency, `disabled readonly`.
        """
        return [
            f
            for f in self.order
            if f not in self.attempted
            and f in self.present
            and not self.controls[f].disabled
            and not self.controls[f].additionalRow
            and self.controls[f].locator
        ]

    def half_walked(self) -> list[str]:
        """fieldIds of gates with a side still untaken, in the order seen.

        A gate declared unexplored is never one: `declare_unexplored` drops its
        remaining sides, so the page can finish with the reason recorded rather
        than restarting forever on a branch already known to be unreachable.

        Absence is not counted: a gate the page has removed cannot be set, and
        `remaining_absent` is what reports the side it still owes. Neither is a
        pinned gate: it is holding a branch open for a deeper gate this walk.
        """
        return [
            f
            for f in self.order
            if f in self.gates
            and self.gates[f].remaining
            and f in self.present
            and f not in self.pinned
        ]

    def remaining_absent(self) -> list[str]:
        """fieldIds of gates owing a side while off the page, in the order seen.

        A gate revealed only under another gate's branch owes its second side
        from a page that no longer renders it. The side is reachable, but only
        after the revealing gate is set back, so it is neither assignable now
        nor walked.
        """
        return [
            f
            for f in self.order
            if f in self.gates and self.gates[f].remaining and f not in self.present
        ]

    def record_fill(self, field_id: str) -> None:
        """Mark `field_id` attempted in the current walk."""
        self.attempted.add(field_id)
        self.walk_of[field_id] = self.walk

    def restart_for(self, walk: int) -> None:
        """Join route `walk`, dropping what was recorded on the route being left.

        The attempt record is cleared because the replay refills the prefix and
        the pages past the branch point are re-rendered by the owed side: a
        field left marked attempted would never be answered on the new branch.
        Gate sides already taken are kept, so a gate is not walked twice down
        the same side.

        `restarts` counts only the restarts issued for this page's own gates,
        which is what `MAX_RESTARTS` bounds; a board dragged onto a new route by
        a restart for another page's gate is not charged for it.
        """
        self.walk = walk
        self.attempted.clear()
        self.advanced.clear()
        self.pinned.clear()
        self.error_reopens.clear()
        self.exhausted.clear()

    def charge_restart(self) -> None:
        """Count one restart against this page's own budget."""
        self.restarts += 1

    def declare_unexplored(self, field_id: str, reason: str) -> None:
        """Record why a gate's owed side was never walked, and stop owing it.

        The sides still remaining are dropped: a walk that cleared `attempted`
        would otherwise assign the gate its owed side directly on the next pass,
        which is the dirty-page fill the restart exists to avoid.
        """
        self.unexplored[field_id] = reason
        gate = self.gates.get(field_id)
        if gate is not None:
            gate.remaining.clear()

    def summary(self) -> dict:
        """Board state for logging and for the completion assertion."""
        return {
            "stageId": self.stage_id,
            "controls": len(self.controls),
            "attempted": sorted(self.attempted),
            "unattempted": self.unattempted(),
            "gates": {
                f: {"walked": g.walked, "remaining": g.remaining} for f, g in self.gates.items()
            },
            "revealed": dict(self.revealed),
            "present": sorted(self.present),
            "pinned": dict(self.pinned),
            "remainingAbsent": self.remaining_absent(),
            "walk": self.walk,
            "walkOf": dict(self.walk_of),
            "restarts": self.restarts,
            "unexplored": dict(self.unexplored),
        }
