from trailblazer.contracts import (
    Assignment,
    Diff,
    FillPageAssignment,
    FrontierBoard,
    Gate,
    PageDescription,
    SetOptionAssignment,
    SimpleAssignment,
    WalkSlice,
    WalkStep,
)


class FrontierBoardState:
    """Stateful board manager for Frontier. Tracks gates, walked/pending options."""

    def __init__(self) -> None:
        self.board = FrontierBoard(currentStageId="", status="exploring")
        self.walk_log: WalkSlice = []

    def identify_gates(self, page: PageDescription) -> list[Gate]:
        """
        Extract potential gates from candidateGates in the page.
        A gate is created if the control is select/toggle with 2+ options.
        """
        gates: list[Gate] = []
        control_map = {c.fieldId: c for c in page.controls}

        for field_id in page.candidateGates:
            if field_id not in control_map:
                continue
            control = control_map[field_id]

            if control.type not in ("select", "toggle") or not control.options:
                continue

            if len(control.options) < 2:
                continue

            kind = "last-page" if page.next is None else "same-page"
            gate_id = f"g_{field_id}"

            gate = Gate(
                gateId=gate_id,
                fieldId=field_id,
                stageId=page.stageId,
                kind=kind,
                options=control.options,
                walked=[],
                pending=list(control.options),
            )
            gates.append(gate)

        return gates

    def next_assignment_for_page(self, page: PageDescription) -> Assignment:
        """
        Decide the next single assignment given the current page.
        Logic:
        1. If blockers exist -> mark blocked, stop.
        2. If current gate has pending options -> pop one, emit set_option.
        3. Else if unfilled required fields -> emit fill_page.
        4. Else -> emit next or submit.
        """
        if page.blockers:
            self.board.status = "blocked"
            return SimpleAssignment(type="stop")

        # Try to find a gate on the current stage with pending options
        active_gate = None
        for gate in self.board.gates:
            if gate.stageId == page.stageId and gate.pending:
                active_gate = gate
                break

        if active_gate:
            # Pop one pending option and emit set_option
            option = active_gate.pending.pop(0)
            locator = self._find_control_locator(page, active_gate.fieldId)
            self.board.status = "awaiting_fill"
            return SetOptionAssignment(
                type="set_option",
                gateId=active_gate.gateId,
                option=option,
                locator=locator,
            )

        # No active gate: check for unfilled required fields
        required_fields = self._unfilled_required_fields(page)
        if required_fields:
            applicant_slice = {f.fieldId: "" for f in required_fields}
            self.board.status = "awaiting_fill"
            return FillPageAssignment(
                type="fill_page",
                applicantSlice=applicant_slice,
            )

        # All fields handled: advance or submit
        if page.next:
            self.board.status = "advancing"
            return SimpleAssignment(type="next")
        else:
            self.board.status = "complete"
            return SimpleAssignment(type="submit")

    def apply_diff(
        self, diff: Diff, last_assignment: Assignment
    ) -> tuple[Assignment | WalkSlice, bool]:
        """
        React to a diff after FormFiller executed the last assignment.
        Returns (next_action, is_walk_slice).

        - +ve: page changed -> move option to walked, continue walking this gate.
        - -ve: page settled -> mark slice_stable, return walk slice, prepare backtrack.

        NOTE: v0 does not implement backtracking or multi-gate. Multi-gate support
        and backtrack are v1 tasks.
        """
        if diff.polarity == "+ve":
            # Page changed: update the gate's walked/pending
            if isinstance(last_assignment, SetOptionAssignment):
                for gate in self.board.gates:
                    if gate.gateId == last_assignment.gateId:
                        gate.walked.append(last_assignment.option)
                        break

            # Continue: next assignment will walk the same gate's next pending option
            # or move on to the next decision. Return a dummy next assignment.
            self.board.status = "exploring"
            return (SimpleAssignment(type="stop"), False)

        else:  # -ve: page settled
            # Build and return the walk slice
            self.board.status = "slice_stable"
            walk_slice = self._build_walk_slice(last_assignment)
            return (walk_slice, True)

    def _find_control_locator(self, page: PageDescription, field_id: str) -> str:
        """Locate a control by fieldId."""
        for control in page.controls:
            if control.fieldId == field_id:
                return control.locator
        raise ValueError(f"Control {field_id} not found on page {page.stageId}")

    def _unfilled_required_fields(
        self, page: PageDescription
    ) -> list[type]:  # Return Control-like objects
        """Return required controls that are not gates and not yet filled."""
        unfilled = []
        gate_field_ids = {g.fieldId for g in self.board.gates}

        for control in page.controls:
            if control.required and control.fieldId not in gate_field_ids:
                unfilled.append(control)

        return unfilled

    def _build_walk_slice(self, last_assignment: Assignment) -> WalkSlice:
        """
        Build the walk slice from the accumulated log.
        v0: returns a minimal walk slice with just the walk_log content.
        v1 will enhance this to include full history of the walk.
        """
        # TODO: v1 — accumulate full walk history (fill, choose, click, etc.)
        # For now, return empty or a single step representing the slice.
        return self.walk_log
