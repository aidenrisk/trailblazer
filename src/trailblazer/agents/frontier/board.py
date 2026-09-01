"""
Core logic for Frontier agent: state machine and assignment decisions.

This module contains the "brain" of Frontier: pure, deterministic functions
that decide what FormFiller should do next, given the current page and what changed
after the last action.

Key concept: FrontierBoardState maintains a "board" (in-memory state) and makes
decisions by asking questions like:
  1. Are there gates on this page I need to explore?
  2. Have I already tried this gate option before?
  3. Did the page change after the last click?
  4. Are there unfilled required fields?
  5. Can I proceed to the next page?

No I/O, no HTTP calls, no AI/LLM — just logic based on form structure.
"""

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
    """
    Manages Frontier's decision logic and internal state (the "board").

    The board tracks:
    - Which gates (branching choices) exist on the form
    - Which gate options have been tried (walked) vs. still need trying (pending)
    - Current page stage
    - Overall status (exploring, awaiting fill, complete, etc.)

    This class is NOT a database or API client — it's pure state + logic.
    Input: PageDescription or Diff
    Output: next Assignment to give to FormFiller, or a WalkSlice to give to ReplayGen

    Core methods:
    - identify_gates(): find branching points in a page
    - next_assignment_for_page(): decide the single next action
    - apply_diff(): react to what changed, update state accordingly
    """

    def __init__(self) -> None:
        self.board = FrontierBoard(currentStageId="", status="exploring")
        self.walk_log: WalkSlice = []  # TODO (v1): accumulate the full walk history here

    def identify_gates(self, page: PageDescription) -> list[Gate]:
        """
        Extract branching points (gates) from a page.

        A gate is a select/toggle field with 2+ options that we'll explore all paths for.
        Scraper gives us candidateGates (hints: "these fields look like gates"),
        we validate and turn each into a Gate with pending/walked tracking.

        Why only select/toggle? Because only those have distinct options to walk.
        Why 2+? Because 1 option isn't a choice.

        Returns: list of Gate objects ready to walk.
        """
        gates: list[Gate] = []
        # Build a lookup map: fieldId → Control (faster than searching list each time)
        control_map = {c.fieldId: c for c in page.controls}

        for field_id in page.candidateGates:
            # Step 1: Is this control even on the page?
            if field_id not in control_map:
                continue
            control = control_map[field_id]

            # Step 2: Is it a select/toggle with actual options?
            if control.type not in ("select", "toggle") or not control.options:
                continue

            # Step 3: Are there at least 2 options to choose between?
            if len(control.options) < 2:
                continue

            # Step 4: What kind of gate is this?
            # If the page has a Next button, clicking this option stays on same page (reveals fields).
            # If no Next button, this is the last page (choices just vary the final walk).
            kind = "last-page" if page.next is None else "same-page"
            gate_id = f"g_{field_id}"

            # Step 5: Create the gate with all options pending (not walked yet).
            gate = Gate(
                gateId=gate_id,
                fieldId=field_id,
                stageId=page.stageId,
                kind=kind,
                options=control.options,
                walked=[],  # None tried yet
                pending=list(control.options),  # All still to try
            )
            gates.append(gate)

        return gates

    def next_assignment_for_page(self, page: PageDescription) -> Assignment:
        """
        Decide what FormFiller should do next. Always returns exactly one action.

        Decision tree (checked in order):
        1. Is the page blocked (validation error, decline, overlay)?
           → Mark as blocked, stop here.
        2. Is there a gate on this page with options still pending?
           → Pick one, pop it from pending, tell FormFiller to click it.
           → This is how we walk: click option 1, see what happens, then click option 2, etc.
        3. Are there required fields on the page that aren't gates?
           → Fill them all at once with dummy values (Loop will provide real values).
        4. Otherwise:
           → If there's a Next button, click it (advance to next page).
           → If there's no Next button, we're at the end: submit the form.

        Returns: One Assignment (never multiple, never zero).
        """
        # Check 1: Blockers (errors, validation issues, decline messaging)?
        if page.blockers:
            self.board.status = "blocked"
            return SimpleAssignment(type="stop")

        # Check 2: Is there a gate on this page with pending options?
        # Frontier enforces: only one gate "in motion" at a time. Don't interleave gate options.
        active_gate = None
        for gate in self.board.gates:
            # Is this gate on the current page AND has options left to try?
            if gate.stageId == page.stageId and gate.pending:
                active_gate = gate
                break  # Take the first one found (v0: only one gate per page anyway)

        if active_gate:
            # Pop the first pending option and tell FormFiller to select it.
            # After FormFiller clicks this, Loop will compare page before/after (Diff),
            # and call apply_diff() to move this option to "walked".
            option = active_gate.pending.pop(0)
            locator = self._find_control_locator(page, active_gate.fieldId)
            self.board.status = "awaiting_fill"
            return SetOptionAssignment(
                type="set_option",
                gateId=active_gate.gateId,
                option=option,
                locator=locator,
            )

        # Check 3: Unfilled required fields (not gates)?
        required_fields = self._unfilled_required_fields(page)
        if required_fields:
            # Collect all required non-gate fields and ask FormFiller to fill them.
            # Values are placeholders; Loop/applicant will provide real data.
            applicant_slice = {f.fieldId: "" for f in required_fields}
            self.board.status = "awaiting_fill"
            return FillPageAssignment(
                type="fill_page",
                applicantSlice=applicant_slice,
            )

        # Check 4: No gates, no unfilled fields. Time to move forward.
        if page.next:
            # Next page exists: click Next to advance.
            self.board.status = "advancing"
            return SimpleAssignment(type="next")
        else:
            # No Next button: this is the last page. Submit the form.
            self.board.status = "complete"
            return SimpleAssignment(type="submit")

    def apply_diff(self, diff: Diff, last_assignment: Assignment) -> bool:
        """
        React to what changed (or didn't change) after FormFiller executed the last assignment.

        Diff polarity tells us:
        - "+ve" (positive): page changed after the click. More work to do on this option.
                           Example: clicking "LLC" revealed new fields (LLC members).
                           Action: Mark option as walked, keep exploring, try next option.
        - "-ve" (negative): page didn't change (or settled). We're done with this option.
                           Example: clicking "next" didn't navigate (validation blocked it).
                           Action: This walk is complete, ready to return.

        Returns: is_walk_complete (bool)
                 True = walk is complete, ready to finalize (call _build_walk_slice())
                 False = walk is still in progress, call next_assignment_for_page() again

        NOTE: v0 does not implement backtracking or multi-gate juggling.
        When a gate is fully walked (-ve diff after last option), v0 just stops.
        v1 will add: backtrack, reset baseline, start next gate.
        """
        if diff.polarity == "+ve":
            # Page changed! The click had an effect.
            # Move the option from "pending" to "walked" so we don't try it again.
            if isinstance(last_assignment, SetOptionAssignment):
                for gate in self.board.gates:
                    if gate.gateId == last_assignment.gateId:
                        # Record that we've successfully walked this option.
                        gate.walked.append(last_assignment.option)
                        break

            # Continue exploring this gate: next_assignment_for_page() will pick
            # the next pending option, or move on if this gate is done.
            self.board.status = "exploring"
            return False  # Walk still in progress

        else:  # -ve: page settled (no change)
            # Page didn't change. This walk is stable.
            # Signal that the walk is complete.
            self.board.status = "slice_stable"
            return True  # Walk complete, ready to return

    def _find_control_locator(self, page: PageDescription, field_id: str) -> str:
        """
        Find a control by fieldId and return its Playwright locator.
        Used by next_assignment_for_page() to get the address for clicking/filling.
        """
        for control in page.controls:
            if control.fieldId == field_id:
                return control.locator
        # If we get here, something went wrong (fieldId claimed but not on page).
        raise ValueError(f"Control {field_id} not found on page {page.stageId}")

    def _unfilled_required_fields(
        self, page: PageDescription
    ) -> list[type]:  # Return Control-like objects
        """
        Return required fields that are NOT gates and haven't been filled yet.

        Why exclude gates? Because gates are handled separately (set_option logic),
        not via fill_page. So this returns only the "free-form" required fields
        (text inputs, date pickers, etc.) that need filling but aren't branching points.
        """
        unfilled = []
        # Build a set of fieldIds that are gates for quick lookup.
        gate_field_ids = {g.fieldId for g in self.board.gates}

        for control in page.controls:
            # Is it required AND not a gate?
            if control.required and control.fieldId not in gate_field_ids:
                unfilled.append(control)

        return unfilled

    def compare_pages(
        self, page_before: PageDescription, page_after: PageDescription
    ) -> Diff:
        """
        Compare two PageDescriptions (before and after a FormFiller action).
        Returns a Diff describing what changed.

        v0: simple heuristic — if same number of controls, assume -ve; if more, assume +ve.
        v1: actual diff logic (compare control fieldIds, detect added/removed/changed).
        """
        # Simple heuristic: if page grew in controls, something changed (+ve)
        if len(page_after.controls) > len(page_before.controls):
            return Diff(polarity="+ve")
        # Otherwise, assume page settled (-ve)
        return Diff(polarity="-ve")

    def _build_walk_slice(self, last_assignment: Assignment) -> WalkSlice:
        """
        Compile the walk slice from this page's actions.
        A walk slice is the ordered sequence of actions that led to a stable walk.

        v0: returns walk_log (currently empty, placeholder for v1).
        v1: will track all actions taken (set option, fill field, click next, etc.)
            and return the full history so ReplayGen can script it.
        """
        # TODO (v1): Accumulate every action taken (choose, type, click, wait) into walk_log
        # as we make assignments. For now, return the empty log.
        return self.walk_log
