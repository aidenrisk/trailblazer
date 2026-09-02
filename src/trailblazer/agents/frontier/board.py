"""
Frontier's board: exploration state for every control, plus the logic over it.

Pure state + logic. No I/O, no LLM, no side effects. Frontier's LangGraph nodes
(frontier.py) are thin wrappers over the methods here, which makes every rule in
this file testable on its own.

The core idea: explore EVERY control, one at a time, in page order. Frontier
doesn't need to know in advance which controls branch. A control turns out to be
a chooser either because Scraper reported its options, or because FormFiller
discovered them while trying to fill it. Either way, all of its options must be
walked before Frontier moves on.
"""

import logging
import re
from typing import Callable, Literal

from pydantic import BaseModel

from trailblazer.contracts import (
    LOGIN_EMAIL,
    LOGIN_OTP,
    LOGIN_PASSWORD,
    Assignment,
    Control,
    ControlState,
    FillFieldAssignment,
    FillReport,
    FrontierBoard,
    Option,
    PageDescription,
    RevealedBy,
    SetOptionAssignment,
    Walk,
    WalkPath,
    WalkSlice,
    WalkStep,
)
from trailblazer.contracts.page_description import is_login_stage

logger = logging.getLogger(__name__)

ValueProvider = Callable[[Control | ControlState], str]

# Which credential fills which kind of login control. Frontier only ever names the
# key; FormFiller resolves it, so no secret passes through the board.
CREDENTIAL_KEY_FOR = {"username": LOGIN_EMAIL, "password": LOGIN_PASSWORD, "otp": LOGIN_OTP}

# The one option Frontier will pick on a login page: the email delivery channel
# for a one-time code, because email is the only channel the shared inbox reads.
_EMAIL_OPTION = re.compile(r"\be-?mail\b", re.IGNORECASE)


def email_option(options: list[Option]) -> Option | None:
    """The option that routes a one-time code to email, if the chooser offers one."""
    return next((o for o in options if _EMAIL_OPTION.search(o.label)), None)


def synthetic_value(control: Control | ControlState) -> str:
    """
    Pick a value to type into a plain field.

    Type-aware so the fill actually lands: a date picker rejects "Test Value",
    a number field rejects it too, and an email field usually validates format.
    Label-sniffing is a heuristic, deliberately — this is capture-time
    exploration, not real applicant data. Real values arrive later, when the
    canonical/applicant mapping exists (see MASTER.md's `canonical` field on
    WalkStep); this function is the injectable seam where that plugs in.
    """
    label = control.label.lower()

    if control.type == "date":
        return "01/01/2026"
    if control.type == "number":
        if "premium" in label:
            return "10000"
        return "1000"

    # text/select/toggle/other: sniff the label for a format the field will accept
    if "email" in label:
        return "test@example.com"
    if "zip" in label or "postal" in label:
        return "10001"
    if "phone" in label:
        return "5551234567"
    if "fein" in label or "ein" in label or "tax id" in label:
        return "12-3456789"
    return "Test Value"


class LoggedAction(BaseModel):
    """
    One action that landed, tagged with enough context to rebuild branches.

    Exploration walks a chooser's options in place — Male, then Female, on the
    same page. That's the cheapest way to see what each option does, but the
    resulting linear log is not replayable: a script that clicks Male then
    Female ends on Female, so the Male branch is never exercised.

    So each entry records which branch it belongs to, and build_walk()
    reconstructs one path per branch out of the single log.

    Attributes:
    - step: the WalkStep itself
    - choiceFor: fieldId, if this step IS an option choice for that control
    - requires: the gate condition this step depends on. A field revealed by
                q_gender == "Female" belongs only to the Female path.
    - phase: "login" for a step taken on a login_* stage. Login steps are
             unconditional and never branch, so build_walk() lifts them out into
             Walk.login instead of threading them through every path.
    """

    step: WalkStep
    choiceFor: str | None = None
    """Board key (stage|locator) of the control this step chose an option for."""
    requires: "Requires | None" = None
    phase: Literal["login", "form"] = "form"


class Requires(BaseModel):
    """The board-keyed form of `RevealedBy`: this step exists only where `key` == `equals`.

    Keyed by (stage, locator) rather than fieldId because the Scraper renumbers
    fieldIds on every look; a locator names the same control every time.
    """

    key: str
    equals: str


def board_key(stage_id: str, locator: str) -> str:
    """The identity Frontier tracks a control by.

    Not `fieldId`: that is a per-perceive counter, so the second page's `q_001`
    is a different control from the first page's, and a control revealed
    mid-page renumbers everything after it. `diff.py` aligns on locator for
    the same reason. The stage is part of the key so `#password` on a second
    host is a second control.
    """
    return f"{stage_id}|{locator}"


class FrontierBoardState:
    """
    Frontier's memory and decision logic.

    Owns:
    - self.board: a ControlState per control seen, in the order first seen
    - self.walk_log: every action that landed, in order — this becomes the WalkSlice

    The four methods below are the whole algorithm:
      sync_controls()       learn what's on the page (including newly revealed fields)
      absorb_fill_report()  learn what FormFiller did and discovered
      select_control()      pick the one control to work on next
      assignment_for()      turn that control into a single Assignment
    """

    def __init__(self, value_provider: ValueProvider | None = None) -> None:
        self.board = FrontierBoard(currentStageId="", status="exploring")
        # Every action that landed, in order, tagged by branch. build_walk()
        # turns this into one replayable path per branch.
        self.action_log: list[LoggedAction] = []
        # Stages we've already clicked Next on. If we're still on a stage we
        # already tried to leave, the click didn't navigate — see
        # note_advance_attempt().
        self.advanced_from: set[str] = set()
        # For a login stage, WHICH credential controls were on the page when Next
        # was clicked. The same stage coming back with the same credentials means
        # the portal rejected them; coming back with different ones (a password
        # field appearing after the username) means the login moved on.
        self.login_signatures: dict[str, frozenset] = {}
        self.value_provider: ValueProvider = value_provider or synthetic_value

    @property
    def walk_log(self) -> WalkSlice:
        """
        Every landed step in observed order, branches interleaved.

        Useful for logging and for asserting what happened; NOT replayable —
        use build_walk() for that. Kept as a property so there's exactly one
        source of truth (action_log).
        """
        return [entry.step for entry in self.action_log]

    # ------------------------------------------------------------------
    # Learning what's on the page
    # ------------------------------------------------------------------

    def sync_controls(self, page: PageDescription) -> list[ControlState]:
        """
        Reconcile the board with a fresh PageDescription.

        Two jobs:
        1. Track any control we haven't seen before. This is how fields revealed
           mid-walk (e.g. "LLC Members" appearing after choosing LLC) join the
           exploration queue — they get appended, so they're explored before the
           page is considered done.
        2. Pick up options the PD now reports for a control whose options we
           didn't know. Scraper may learn a widget's options on a later look
           (once FormFiller has opened it), and that's just as good as
           FormFiller telling us directly.

        We derive "what's new" by comparing against the board rather than
        reading Diff.addedControls — the board is the source of truth about what
        we've already seen, so this stays correct even if the diff is imprecise.

        Returns: the ControlStates that were newly added (for logging).
        """
        self.board.currentStageId = page.stageId
        by_key = {self._key_of(c): c for c in self.board.controls}
        added: list[ControlState] = []

        for control in page.controls:
            entry = by_key.get(board_key(page.stageId, control.locator))
            if entry is None:
                entry = ControlState(
                    fieldId=control.fieldId,
                    label=control.label,
                    stageId=page.stageId,
                    locator=control.locator,
                    type=control.type,
                    required=control.required,
                    explored=False,
                    options=control.options,
                    walked=[],
                    # If Scraper already gave us the options, they're all pending.
                    # If not (options is None), pending stays empty and this
                    # control gets a fill_field first — which is how FormFiller
                    # gets the chance to discover that it's really a chooser.
                    pending=list(control.options) if control.options else [],
                    # Carried through so an action on this control can be
                    # attributed to the branch that revealed it.
                    revealedBy=control.revealedBy,
                    credential=control.credential,
                )
                if is_login_stage(page.stageId):
                    self._apply_login_policy(entry)
                self.board.controls.append(entry)
                by_key[self._key_of(entry)] = entry
                added.append(entry)
                continue

            # Already tracked at this stage and locator. The Scraper renumbers
            # fieldIds on every look, so keep ours current -- Assignments and
            # WalkSteps name the control by the id the page has right now.
            if entry.fieldId != control.fieldId:
                logger.debug("%s renumbered %s -> %s", entry.locator, entry.fieldId, control.fieldId)
                entry.fieldId = control.fieldId
            if control.revealedBy is not None and entry.revealedBy is None:
                entry.revealedBy = control.revealedBy

            # Did the PD just teach us its options?
            if entry.options is None and control.options is not None:
                self._set_options(entry, control.options, chosen=None)
                if is_login_stage(entry.stageId):
                    self._apply_login_policy(entry)
                logger.info(
                    "Scraper reported options for %s: %s",
                    entry.fieldId,
                    [o.label for o in control.options],
                )

        if added:
            logger.info("New controls to explore: %s", [c.fieldId for c in added])
        return added

    def absorb_fill_report(
        self, report: FillReport, last_assignment: Assignment | None
    ) -> None:
        """
        Update the board from what FormFiller just did.

        This is the FormFiller -> Frontier channel. It arrives via Loop (agents
        never call each other), but the information is the filler's:

        - fill_field + discoveredOptions is not None
            FormFiller found out this control is actually a chooser. Record its
            options, mark the one it picked as walked, and leave explored=False
            so the remaining options get walked before we move on.
        - fill_field + discoveredOptions is None
            Plain field. One fill and it's done.
        - set_option
            Move that option pending -> walked. Explored once pending is empty.

        A failed report (ok=False) teaches us nothing about the control, so we
        record nothing — Loop stops the walk on failure anyway.
        """
        if not report.ok:
            logger.warning(
                "FormFiller failed on %s: %s", report.fieldId, report.errorClass
            )
            return

        if isinstance(last_assignment, FillFieldAssignment):
            entry = self._entry_at(last_assignment.locator, last_assignment.fieldId)
            if entry is None:
                return

            if last_assignment.credentialKey is not None:
                # A credential is filled once, from the store. Whatever FormFiller
                # saw while doing it (an autocomplete, a chooser) is not walked:
                # there is nothing to explore about a password field.
                entry.options = []
                entry.explored = True
                self._log_fill(entry, None, credential_key=last_assignment.credentialKey)
            elif report.discoveredOptions is not None:
                # The surprise case: what looked like a plain field is a chooser.
                self._set_options(
                    entry, report.discoveredOptions, chosen=report.chosenOption
                )
                logger.info(
                    "FormFiller discovered %s is a chooser: options=%s chose=%s -> "
                    "%d still pending",
                    entry.fieldId,
                    [o.label for o in report.discoveredOptions],
                    report.chosenOption,
                    len(entry.pending),
                )
                self._log_choice(entry, report.chosenOption)
            else:
                # Plain field: filled, done.
                entry.options = []
                entry.explored = True
                self._log_fill(entry, last_assignment.value)

        elif isinstance(last_assignment, SetOptionAssignment):
            # The option's own locator is what was clicked; the CONTROL is what
            # the board tracks, so resolve by controlLocator.
            entry = self._entry_at(last_assignment.controlLocator, last_assignment.fieldId)
            if entry is None:
                return
            self._walk_option(entry, last_assignment.option)
            if is_login_stage(entry.stageId):
                # One choice, never a walk: the email channel was picked so the
                # code lands where the inbox can read it. The SMS option is not
                # a branch of the form; trying it would burn a code we cannot see.
                entry.pending = []
                entry.explored = True
            self._log_choice(entry, last_assignment.option)
            logger.info(
                "Walked %s=%s (%d pending, explored=%s)",
                entry.fieldId,
                last_assignment.option,
                len(entry.pending),
                entry.explored,
            )

    # ------------------------------------------------------------------
    # Deciding what to do next
    # ------------------------------------------------------------------

    def select_control(self, page: PageDescription) -> ControlState | None:
        """
        Pick the ONE control to work on next, or None if this page is done.

        Two priorities, in order:

        1. A control that exists only because of the option currently set, and
           isn't fully explored yet. It has to be done NOW, before we change
           that option, because changing it makes the field disappear. Choosing
           "LLC" reveals "Number of members"; if we move on to "Corporation"
           first, the members field is gone and we've lost that branch.

           This includes a revealed control that is itself a chooser: all of ITS
           options get walked while the option that revealed it is still set.

        2. Otherwise, the first control in page order that is unexplored or has
           options left to try.

        Priority 2 needs no special case for "finish this control's options
        before starting the next one": a control with pending options isn't
        explored, and it appears earlier in the list, so it's simply found
        first. Priority 1 is the one real exception to page order.
        """
        on_stage = [c for c in self.board.controls if c.stageId == page.stageId]

        for entry in on_stage:
            # Not `explored or pending` — a revealed control whose options are
            # already known (so pending is non-empty) is exactly as ephemeral as
            # one we have to discover. Skipping it here meant a revealed
            # dropdown got walked last, after its enabling option had been
            # changed and the control no longer existed.
            if entry.explored:
                continue
            if self._is_live_reveal(entry):
                logger.info(
                    "%s is revealed by %s=%s which is set now - exploring before "
                    "that changes",
                    entry.fieldId,
                    entry.revealedBy.fieldId,
                    entry.revealedBy.equals,
                )
                return entry

        for entry in on_stage:
            if entry.pending or not entry.explored:
                return entry
        return None

    def _is_live_reveal(self, entry: ControlState) -> bool:
        """
        Does this control exist only because of an option that is set right now?

        `walked[-1]` is whatever the revealing chooser currently has selected,
        since options are walked in order and the last one wins.
        """
        if entry.revealedBy is None:
            return False
        revealer = self._entry(entry.revealedBy.fieldId, entry.stageId, warn=False)
        if revealer is None or not revealer.walked:
            return False
        return revealer.walked[-1].label == entry.revealedBy.equals

    def assignment_for(self, entry: ControlState) -> Assignment:
        """
        Turn the selected control into exactly one Assignment.

        - A credential control -> fill_field naming the credential, never a value.
        - Options known and some pending -> set_option for the next one.
        - Otherwise -> fill_field with a synthetic value. If the control turns
          out to be a chooser, FormFiller tells us and we come back here for
          each remaining option.
        """
        if entry.credential is not None:
            return FillFieldAssignment(
                type="fill_field",
                fieldId=entry.fieldId,
                locator=entry.locator,
                credentialKey=CREDENTIAL_KEY_FOR[entry.credential],
            )

        if entry.pending:
            option = entry.pending[0]
            # The option's own locator is what FormFiller must act on: a native
            # <select> addresses each <option> separately, and a split control
            # (paired Yes/Maybe buttons) has a distinct locator per button.
            # Options discovered by FormFiller may carry no distinct locator of
            # their own, in which case they reuse the control's.
            return SetOptionAssignment(
                type="set_option",
                fieldId=entry.fieldId,
                option=option.label,
                locator=option.locator or entry.locator,
                controlLocator=entry.locator,
            )

        return FillFieldAssignment(
            type="fill_field",
            fieldId=entry.fieldId,
            locator=entry.locator,
            value=self.value_provider(entry),
        )

    def note_advance_attempt(
        self, stage_id: str, locator: str, page: PageDescription | None = None
    ) -> None:
        """
        Record that we're clicking Next to leave this stage.

        The click is logged optimistically, because we won't know whether it
        navigated until the next look. If it turns out it didn't,
        discard_unlanded_navigation() takes it back out — the walk slice is a
        record of what landed, not of what we attempted.

        On a login stage the page is remembered too, so a return to the same
        stage can be read: same credential controls means rejected, different
        ones means the login moved to its next step.
        """
        self.advanced_from.add(stage_id)
        login = is_login_stage(stage_id)
        if login and page is not None:
            self.login_signatures[stage_id] = self._login_signature(page)
        self.action_log.append(
            LoggedAction(
                step=WalkStep(action="click", locator=locator),
                phase="login" if login else "form",
            )
        )

    def already_tried_to_advance(self, stage_id: str) -> bool:
        return stage_id in self.advanced_from

    # ------------------------------------------------------------------
    # Login stages
    # ------------------------------------------------------------------

    def _apply_login_policy(self, entry: ControlState) -> None:
        """
        Decide, for one control on a login page, the only thing Frontier may do with it.

        A login page is filled, not explored. Credential controls are filled from
        the store (assignment_for names the key). A chooser that offers an email
        channel gets that one option and nothing else. Everything else -- a
        "remember me" toggle, a marketing field, a chooser with no email option,
        a select whose options are unknown -- is left exactly as the portal
        rendered it, because typing into it or walking it changes the login
        rather than the form.
        """
        if entry.credential is not None:
            entry.pending = []
            entry.explored = False
            return
        email = email_option(entry.options) if entry.options else None
        if email is not None and not entry.walked:
            entry.pending = [email]
            entry.explored = False
            return
        entry.pending = []
        entry.explored = True

    @staticmethod
    def _login_signature(page: PageDescription) -> frozenset:
        """Which credential controls a login page carries, by locator and kind."""
        return frozenset((c.locator, c.credential) for c in page.controls if c.credential)

    def login_rejected(self, page: PageDescription) -> bool:
        """
        Did the portal refuse the credentials?

        True when Next was clicked on this login stage and the page has come back
        with the same credential controls it had then: nothing moved, the login
        was rejected. Frontier stops rather than clicking Next again -- every
        retry on some portals costs a one-time code or a lockout counter.
        """
        if not is_login_stage(page.stageId) or page.stageId not in self.advanced_from:
            return False
        return self.login_signatures.get(page.stageId) == self._login_signature(page)

    def login_can_advance_again(self, page: PageDescription) -> bool:
        """
        Is this a login stage that has moved on under the same name?

        A portal that asks for the username, then reveals the password field on
        the same URL, comes back as the same stage with a different credential
        signature. That is progress, not a stuck click, so Next may be clicked
        again once the new control is filled.
        """
        if not is_login_stage(page.stageId) or page.stageId not in self.advanced_from:
            return False
        return self.login_signatures.get(page.stageId) != self._login_signature(page)

    def discard_unlanded_navigation(self) -> None:
        """Drop a trailing navigation click that turned out not to navigate."""
        if self.action_log and self.action_log[-1].step.action == "click":
            dropped = self.action_log.pop()
            logger.warning(
                "Next click did not navigate; dropped %s", dropped.step.locator
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _key_of(entry: ControlState) -> str:
        return board_key(entry.stageId, entry.locator)

    def _entry(
        self, field_id: str, stage_id: str | None = None, warn: bool = True
    ) -> ControlState | None:
        """The control with this fieldId on `stage_id` (default: the current stage).

        fieldIds are only meaningful within one page, so the lookup never crosses
        stages; a `revealedBy.fieldId` always names a control on the same page.
        """
        stage = stage_id if stage_id is not None else self.board.currentStageId
        for entry in self.board.controls:
            if entry.stageId == stage and entry.fieldId == field_id:
                return entry
        if warn:
            logger.warning("No board entry for fieldId %s on %s", field_id, stage)
        return None

    def _entry_at(self, locator: str, field_id: str | None = None) -> ControlState | None:
        """The control at `locator` on the current stage; by fieldId if the locator is unknown.

        Used to attribute a FillReport: the Assignment named a locator, which is
        stable, and a fieldId, which may have been renumbered since.
        """
        key = board_key(self.board.currentStageId, locator)
        for entry in self.board.controls:
            if self._key_of(entry) == key:
                return entry
        if field_id is not None:
            return self._entry(field_id)
        logger.warning("No board entry at %s on %s", locator, self.board.currentStageId)
        return None

    def _requires_of(self, entry: ControlState) -> Requires | None:
        """This control's reveal condition, keyed the way the walk log needs it."""
        if entry.revealedBy is None:
            return None
        revealer = self._entry(entry.revealedBy.fieldId, entry.stageId, warn=False)
        key = (
            self._key_of(revealer)
            if revealer is not None
            else board_key(entry.stageId, entry.revealedBy.fieldId)
        )
        return Requires(key=key, equals=entry.revealedBy.equals)

    def _set_options(
        self, entry: ControlState, options: list[Option], chosen: str | None
    ) -> None:
        """
        Install a control's real options, then account for one already picked.

        An empty list means "confirmed: no options" — the control is a plain
        field after all, and it's explored. Without that case a chooser with
        zero options would be re-filled forever and block every later control
        on the page.
        """
        entry.options = list(options)

        if not options:
            entry.explored = True
            return

        entry.pending = list(options)
        entry.walked = []
        entry.explored = False

        if chosen is not None:
            self._walk_option(entry, chosen)

    def _walk_option(self, entry: ControlState, label: str) -> None:
        """Move one option from pending to walked; explored once none are left."""
        match = next((o for o in entry.pending if o.label == label), None)
        if match is not None:
            entry.pending.remove(match)
            entry.walked.append(match)
        else:
            # FormFiller picked something we didn't have pending (e.g. a label
            # that differs slightly). Record it so we don't lose the walk, but
            # don't let it clear the pending list.
            logger.warning(
                "Option %r not pending on %s (pending=%s)",
                label,
                entry.fieldId,
                [o.label for o in entry.pending],
            )
            entry.walked.append(Option(label=label, locator=entry.locator))

        if not entry.pending:
            entry.explored = True

    def _log_fill(
        self, entry: ControlState, value: str | None, credential_key: str | None = None
    ) -> None:
        login = is_login_stage(entry.stageId)
        self.action_log.append(
            LoggedAction(
                step=WalkStep(
                    action="type",
                    fieldId=entry.fieldId,
                    locator=entry.locator,
                    value=value,
                    credentialKey=credential_key,
                ),
                # Login steps are unconditional: nothing on a login page is a branch.
                requires=None if login else self._requires_of(entry),
                phase="login" if login else "form",
            )
        )

    def _log_choice(self, entry: ControlState, label: str | None) -> None:
        if label is None:
            return
        option = next((o for o in entry.walked if o.label == label), None)
        login = is_login_stage(entry.stageId)
        self.action_log.append(
            LoggedAction(
                step=WalkStep(
                    action="choose",
                    fieldId=entry.fieldId,
                    locator=option.locator if option else entry.locator,
                    option=label,
                ),
                # The email-channel pick on a login page is not a chooser being
                # walked, so it pins no branch and belongs to every path.
                choiceFor=None if login else self._key_of(entry),
                requires=None if login else self._requires_of(entry),
                phase="login" if login else "form",
            )
        )

    # ------------------------------------------------------------------
    # Turning the log into replayable paths
    # ------------------------------------------------------------------

    def build_walk(self) -> Walk:
        """
        Reconstruct one replayable path per branch.

        Enumeration follows MASTER.md's "do not walk combinations of independent
        gates": a baseline path taking each chooser's first walked option, plus
        one variant per remaining option. Choosers with 2/3/2 options give
        1 + 1 + 2 + 1 = 5 paths, not 2 x 3 x 2 = 12.

        Each path keeps the actions that belong to it, in the order they were
        observed. Relative order is preserved, so every path is a subsequence of
        a real execution — never an invented ordering.

        Steps taken on login stages are lifted out first, into Walk.login. They
        never branch, every path would start with them, and they are published
        per carrier rather than per form -- so the paths do not repeat them.
        """
        login_steps = [e.step for e in self.action_log if e.phase == "login"]
        form_steps = [e.step for e in self.action_log if e.phase == "form"]

        # Keyed by (stage, locator), like the log entries that refer to them.
        choosers = {
            self._key_of(c): [o.label for o in c.walked]
            for c in self.board.controls
            if c.walked and not is_login_stage(c.stageId)
        }

        if not choosers:
            # Nothing branched: the log is already a single replayable path. A
            # walk that never left the login has no form path at all.
            if login_steps and not form_steps:
                return Walk(login=login_steps, paths=[])
            return Walk(login=login_steps, paths=[WalkPath(choices={}, steps=form_steps)])

        baseline = {field: labels[0] for field, labels in choosers.items()}
        pinned = [baseline]
        for field, labels in choosers.items():
            for alternative in labels[1:]:
                pinned.append({**baseline, field: alternative})

        paths: list[WalkPath] = []
        seen: set[tuple] = set()

        for choices in pinned:
            steps = self._steps_for(choices, choosers)

            # Two different pins can produce the same script. A chooser that
            # only exists on one branch can't be pinned on the others, so its
            # variants collapse — and identical steps mean an identical
            # Program, so keep one.
            key = tuple(
                (s.action, s.fieldId, s.locator, s.value, s.option) for s in steps
            )
            if key in seen:
                continue
            seen.add(key)

            # Report the choices this path actually MAKES, not the ones we pinned
            # to select it. A path that never reaches a chooser (because the
            # option revealing it isn't on this branch) must not claim to pin it.
            paths.append(
                WalkPath(
                    choices={
                        s.fieldId: s.option
                        for s in steps
                        if s.action == "choose" and s.fieldId and s.option
                    },
                    steps=steps,
                )
            )

        logger.info(
            "Built %d paths from %d logged actions (%d login, choosers: %s)",
            len(paths),
            len(self.action_log),
            len(login_steps),
            {f: len(v) for f, v in choosers.items()},
        )
        return Walk(login=login_steps, paths=paths)

    def _steps_for(
        self, choices: dict[str, str], choosers: dict[str, list[str]]
    ) -> WalkSlice:
        """Keep only the form actions that belong on the given path."""
        steps: WalkSlice = []

        for entry in self.action_log:
            if entry.phase == "login":
                continue  # lifted into Walk.login
            # A choice belongs to this path only if it's the option pinned here.
            if entry.choiceFor is not None:
                if choices.get(entry.choiceFor) != entry.step.option:
                    continue

            # A revealed field belongs only where its condition holds. If the
            # revealing field isn't a chooser we can't pin it, so treat the step
            # as unconditional rather than dropping it from every path.
            if entry.requires is not None and entry.requires.key in choosers:
                if choices.get(entry.requires.key) != entry.requires.equals:
                    continue

            steps.append(entry.step)

        return steps
