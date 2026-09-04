"""The board: what Frontier knows about one page.

The only state in the pipeline. It answers, for one page, which controls exist,
which have been acted on, which two-sided gates remain half-walked, which
controls appeared as a result of which assignment, and which walk each fill
belongs to.

A board covers exactly one `stageId`. Advancing to a new stage retires the
previous board rather than extending it, because `fieldId` is a per-page counter
(`page_description.Control.fieldId`) and carries no cross-page identity.
"""

from dataclasses import dataclass, field

from trailblazer.contracts.page_description import Control

# Types whose two-valued form is a branch rather than a value. `switch` and
# `boolean` from the spec's gate rule are not in `ControlType`, so `toggle` is
# the whole of that clause here.
_GATE_TYPES = {"toggle", "select"}

# The two sides of a gate that carries no options: a checkbox is either set or
# not, and there is no option label to name either state.
CHECKED = "true"
UNCHECKED = "false"


def gate_sides(control: Control) -> list[str] | None:
    """The two values `control` must be walked through, or `None` if it is no gate.

    Decided by shape, not by type name (spec §4, "What counts as a gate"): a
    `toggle` is always a gate, a control with no options is a checkbox-shaped
    gate, and anything choice-bearing with exactly two options is a gate. A
    control with three or more options is walked once -- the walk covers
    branches, not combinations.
    """
    if control.options is None:
        # `text`/`number`/`date` are barred from options by the contract, so an
        # optionless control here is a toggle: a checkbox with two implicit sides.
        return [CHECKED, UNCHECKED] if control.type == "toggle" else None
    if control.type in _GATE_TYPES and len(control.options) == 2:
        return [o.label for o in control.options]
    return None


@dataclass
class GateWalk:
    """One gate's two sides and which of them have been taken."""

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

    dismissed_blockers: set[str] = field(default_factory=set)
    """Locators already targeted by a dismiss `advance`, so one that does not
    clear is not re-issued forever."""

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

    restarts: int = 0
    """Restarts issued on this page, counted against `MAX_RESTARTS`."""

    unexplored: dict[str, str] = field(default_factory=dict)
    """fieldId -> why a gate's owed side was never walked.

    A gate is entered here when the restart cap is reached or when replaying the
    prefix for it failed. Loop copies these into `branchExploration.unexplored`
    when the page finishes, which is what the completion assertion reads.
    """

    def add(self, control: Control, revealed_by: str | None = None) -> bool:
        """Record `control`, returning True when it was not already on the board.

        The control is re-stored on every observe because `options` can change:
        a combobox reports `None` until an `expand` mounts its listbox, and the
        gate decision depends on the option count.
        """
        new = control.fieldId not in self.controls
        if new:
            self.order.append(control.fieldId)
            if revealed_by is not None:
                self.revealed[control.fieldId] = revealed_by
        self.controls[control.fieldId] = control

        sides = None if control.disabled else gate_sides(control)
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
        ]

    def half_walked(self) -> list[str]:
        """fieldIds of gates with a side still untaken, in the order seen.

        A gate declared unexplored is never one: `declare_unexplored` drops its
        remaining sides, so the page can finish with the reason recorded rather
        than restarting forever on a branch already known to be unreachable.

        Absence is not counted: a gate the page has removed cannot be set, and
        `remaining_absent` is what reports the side it still owes.
        """
        return [
            f
            for f in self.order
            if f in self.gates and self.gates[f].remaining and f in self.present
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

    def restart(self) -> int:
        """Open the next walk and return its id.

        The attempt record is cleared because the replay refills the prefix and
        the page past the branch point is re-rendered by the owed side: a field
        left marked attempted would never be answered on the new branch. Gate
        sides already taken are kept, so a gate is not walked twice down the
        same side.
        """
        self.walk += 1
        self.restarts += 1
        self.attempted.clear()
        self.advanced.clear()
        return self.walk

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
            "remainingAbsent": self.remaining_absent(),
            "walk": self.walk,
            "walkOf": dict(self.walk_of),
            "restarts": self.restarts,
            "unexplored": dict(self.unexplored),
        }
