"""The Generator: append one action to all three artifacts, or to none.

Called after every fill, not once per finished page (arch doc 4, step 5). A
branch page must be recorded three times -- its question, its metadata field and
its script block. RoadRunner held that as one rule in one agent's head, the
script part was dropped, and a gate existed in both manifests and in neither
script; the flow quoted correctly until a client answered that gate the other
way.

The fix here is structural rather than procedural: `append` stages all three
writes in memory, and only when all three have been built does anything touch
the disk. There is no code path that writes one and not the others, so the rule
cannot be half-followed. Stage counts are compared across the three at every page
boundary rather than once at the end.

Files are written incrementally, so a run that dies mid-flow leaves partial work
that can be inspected and resumed. Publishing is not this agent's job: Loop runs
the completion assertion, and only a flow that passes it reaches persist.
"""

import json
import re
import time
from pathlib import Path

from trailblazer.agents.generator import script as script_emitter
from trailblazer.agents.generator.artifacts import (
    Blocked,
    Conditional,
    MetadataDoc,
    MetadataField,
    MetadataOption,
    Question,
    QuestionsDoc,
    Stage,
)
from trailblazer.agents.generator.canonical import CanonicalResolver
from trailblazer.agents.generator.reconcile import (
    catalog_type,
    clean_options,
    merge_captures,
    strip_merge_fields,
)
from trailblazer.contracts.generation import GenerationRequest, GenerationState
from trailblazer.contracts.page_description import Control, Option
from trailblazer.observability.ledger import RunLedger
from trailblazer.observability.logging import get_logger

log = get_logger(__name__)

CREDENTIAL_PLACEHOLDERS = ("$EMAIL", "$PASSWORD", "$OTP")
"""What a credential looks like everywhere in the artifacts. Never the literal."""

# Anything that looks like a real secret rather than a placeholder. Persist runs
# its own leak scan and throws; this catches it at write time, where the page
# that produced it is still identifiable.
_SECRET_KEYS = re.compile(r"password|passwd|secret|token|otp|api[_-]?key", re.IGNORECASE)


class CredentialLeak(RuntimeError):
    """A credential literal reached an artifact. Never written, always raised."""


class ArtifactMismatch(RuntimeError):
    """The three artifacts disagree. Raised at the page boundary that produced it."""


def _unit_and_format(constraint: dict[str, str] | None) -> tuple[str | None, str | None, str | None]:
    """Split a filler-discovered constraint into `unit`, `format` and `answerHint`.

    Discovered only by being rejected: it is in neither the PageDescription nor
    the assignment, and it is what lets a different answer at replay time be
    shaped correctly instead of failing the same validation.
    """
    if not constraint:
        return None, None, None
    return constraint.get("unit"), constraint.get("format"), constraint.get("hint")


def _infer_unit(label: str, control: Control | None) -> str | None:
    """Guess the value's unit from the label, when no rejection revealed one.

    `unit` beats the HTML type in the catalog mapping, so a dollar amount typed
    into a text box still maps to currency.
    """
    lowered = label.lower()
    if any(w in lowered for w in ("payroll", "premium", "revenue", "sales", "cost", "amount", "$")):
        return "usd"
    if "percent" in lowered or "%" in lowered:
        return "percent"
    if "square" in lowered or "sq ft" in lowered or "sqft" in lowered:
        return "sqft"
    if "number of" in lowered or "how many" in lowered or "count" in lowered:
        return "count"
    if control is not None and control.type == "date":
        return "date"
    return None


class Generator:
    """Accumulates the three artifacts for one `(carrier, businessType, insuranceType)` flow.

    Owns `questionId` allocation. `Control.fieldId` is a per-page counter reset
    at every perceive, so it is not cross-page identity and cannot be the join
    key; the mapping from `(stageId, fieldId)` to a `questionId` is held here.
    """

    def __init__(
        self,
        out_dir: Path,
        carrier: str,
        business_type: str,
        insurance_type: str,
        login_url: str = "",
    ) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.carrier = carrier
        self.business_type = business_type
        self.insurance_type = insurance_type

        self.slug = script_emitter.slug(carrier, business_type, insurance_type)
        self.base = f"onboarding-{self.slug}"
        """The runner regex-scrapes the script for `onboarding-*.metadata.json` and
        `onboarding-*.questions.json`, then materializes both under exactly those
        names. Any other naming materializes nothing."""

        self.questions_path = self.out_dir / f"{self.base}.questions.json"
        self.metadata_path = self.out_dir / f"{self.base}.metadata.json"
        self.script_name = f"{carrier}-{business_type}-{insurance_type}-replay-script.js"
        self.script_path = self.out_dir / self.script_name

        self.questions_doc = QuestionsDoc(
            carrier=carrier, businessType=business_type, insuranceType=insurance_type
        )
        self.metadata_doc = MetadataDoc(
            carrier=carrier,
            loginUrl=login_url,
            businessType=business_type,
            insuranceType=insurance_type,
        )
        self.script_blocks: list[str] = []

        self._resolver = CanonicalResolver()
        self._next_question = 1
        self._by_field: dict[tuple[str, str], str] = {}
        """`(stageId, fieldId)` -> questionId. fieldId alone repeats across pages."""

        self._answers: dict[int, dict[str, str]] = {}
        """walk -> questionId -> the value that walk answered with.

        Kept per walk rather than per question because backtracking answers the
        same field once per walk, and the answers assembled across walks are not
        a path the form ever rendered: q_010's answer from the walk where q_009
        was Yes survives even after q_009 is set back to No. `publish_walk`
        fixes one walk's set as `exampleValue`.
        """

        self._published_walk: int | None = None
        """The walk whose answers are currently written as `exampleValue`."""
        self._stage_index: dict[str, int] = {}

    # -- identity ---------------------------------------------------------

    def _question_id(self, stage_id: str, field_id: str) -> tuple[str, bool]:
        """Return the questionId for one control, allocating on first sight.

        Monotonic across the whole flow and stable once allocated. The bool says
        whether this call allocated it, which is what tells `append` it is
        looking at a new question rather than a second capture of one.
        """
        key = (stage_id, field_id)
        if key in self._by_field:
            return self._by_field[key], False
        qid = f"q_{self._next_question:03d}"
        self._next_question += 1
        self._by_field[key] = qid
        return qid, True

    # -- guards -----------------------------------------------------------

    def _assert_no_credential_literal(self, question: Question, field: MetadataField) -> None:
        """Refuse to write anything that looks like a real credential.

        Persist runs a leak scan and throws; catching it here names the page and
        the control that produced it, which the persist-time scan cannot.
        """
        if question.exampleValue and _SECRET_KEYS.search(question.canonical or ""):
            if question.exampleValue not in CREDENTIAL_PLACEHOLDERS:
                raise CredentialLeak(
                    f"{question.questionId} ({question.canonical}) carries a credential "
                    f"literal as exampleValue; expected one of {CREDENTIAL_PLACEHOLDERS}"
                )
        for value in (field.selector, field.selectorYes, field.selectorNo):
            if value and any(p in value for p in ("LOGIN_PASSWORD=", "password=")):
                raise CredentialLeak(f"{field.questionId} selector embeds a credential: {value!r}")

    def _assert_stages_agree(self) -> None:
        """Compare stage counts across the three artifacts at a page boundary.

        A stage in the metadata and not in the script is the defect that let a
        gate exist in both manifests and in neither script. Checked here rather
        than at the end, because at the end the page that caused it is gone.
        """
        metadata_stages = [s.name for s in self.metadata_doc.stages]
        script_stages = re.findall(r"// --- stage: (.+?) ---", "".join(self.script_blocks))
        if metadata_stages != script_stages:
            raise ArtifactMismatch(
                f"stages disagree: metadata={metadata_stages} script={script_stages}"
            )
        question_pages = {q.page for q in self.questions_doc.questions}
        unknown = question_pages - set(metadata_stages)
        if unknown:
            raise ArtifactMismatch(f"questions name stages absent from metadata: {sorted(unknown)}")

    # -- the three-way write ----------------------------------------------

    def _stage_for(self, stage_id: str, url: str) -> Stage:
        """Return the metadata stage for `stage_id`, opening it in all three on first sight."""
        if stage_id in self._stage_index:
            return self.metadata_doc.stages[self._stage_index[stage_id]]
        stage = Stage(name=stage_id, url=url, stageType="form")
        self.metadata_doc.stages.append(stage)
        self._stage_index[stage_id] = len(self.metadata_doc.stages) - 1
        self.script_blocks.append(script_emitter.stage_block(stage_id, url))
        return stage

    def _build_question(
        self,
        qid: str,
        stage_id: str,
        label: str,
        control: Control | None,
        report_value: str | None,
        constraint: dict[str, str] | None,
        options: list[Option] | None,
        open_set: bool,
        required: bool,
        conditional: Conditional | None,
    ) -> Question:
        """Assemble the semantic half of the pair: what the control means."""
        canonical = self._resolver.resolve(label)
        unit, fmt, hint = _unit_and_format(constraint)
        unit = unit or _infer_unit(label, control)

        portal_type = control.type if control else "other"
        option_labels = [o.label for o in options] if options else []
        mapped = catalog_type(
            portal_type=portal_type,
            unit=unit,
            option_count=len(option_labels),
            canonical=canonical,
            label=label,
            open_set=open_set,
        )
        return Question(
            questionId=qid,
            page=stage_id,
            canonical=canonical,
            label=label,
            type=mapped,
            required=required,
            # `options` is NULL unless the final type is enum (arch doc 3.5).
            options=option_labels if mapped == "enum" and option_labels else None,
            openSet=True if open_set else None,
            exampleValue=report_value,
            answerHint=hint,
            unit=unit,
            format=fmt,
            conditional=conditional,
        )

    def _build_metadata_field(
        self, qid: str, locator: str, options: list[Option] | None, is_gate: bool
    ) -> MetadataField:
        """Assemble the technical half: where the control is.

        Three shapes per arch doc 3.4. Label, type, required, option text,
        `exampleValue` and `conditional` are not duplicated here -- they live in
        the questions artifact and the pair joins on `questionId`.
        """
        if options and any(o.locator for o in options):
            if is_gate and len(options) == 2:
                yes, no = options[0], options[1]
                for o in options:
                    if o.label.strip().lower() in ("yes", "true"):
                        yes = o
                    elif o.label.strip().lower() in ("no", "false"):
                        no = o
                return MetadataField(
                    questionId=qid, selectorYes=yes.locator, selectorNo=no.locator
                )
            return MetadataField(
                questionId=qid,
                selector=None,
                options=[MetadataOption(label=o.label, selector=o.locator) for o in options],
            )
        return MetadataField(questionId=qid, selector=locator)

    def append(self, request: GenerationRequest, ledger: RunLedger | None = None) -> GenerationState:
        """Append one action to all three artifacts. All three, or none.

        Everything is built in memory first. Only once the question, the metadata
        field and the script block all exist does anything reach the disk, so
        there is no path that records a branch in two artifacts and not the
        third.

        An action is appended only when known-good: a report whose `ok` is false
        records the blocker and writes no question, because a control that could
        not be set has no verified answer to replay.
        """
        started = time.monotonic()
        report = request.report
        page = request.page
        stage_id = page.stageId

        try:
            stage = self._stage_for(stage_id, page.url)

            if not report.ok:
                # A blocker is a stop condition, not a question. Recorded so the
                # gap is visible rather than silently absent from the artifacts.
                self.metadata_doc.blocked.append(
                    Blocked(
                        page=stage_id,
                        control=request.control_label or report.fieldId or report.locator,
                        questionId=self._by_field.get((stage_id, report.fieldId or "")),
                        whatYouTried=(report.blocked or {}).get("whatYouTried", "unknown"),
                    )
                )
                self._flush()
                self._record(ledger, "blocked", stage_id, started, ok=False)
                return self.state()

            if report.intent == "advance" or report.fieldId is None:
                # An advance has no question: it is the stage's exit, recorded on
                # the stage and in the script so the two stay in step.
                stage.next = report.locator
                self.script_blocks.append(script_emitter.advance_block(report.locator))
                self._flush()
                self._record(ledger, "advance", stage_id, started)
                return self.state()

            qid, is_new = self._question_id(stage_id, report.fieldId)
            if report.valueUsed is not None:
                self._answers.setdefault(request.walk, {})[qid] = report.valueUsed
            control = next((c for c in page.controls if c.fieldId == report.fieldId), None)

            raw_label = request.control_label or (control.label if control else report.fieldId)
            label = strip_merge_fields(raw_label)
            # Options an `expand` revealed outrank the scraper's: a combobox
            # mounts its listbox only on click, so a read-only extraction of it
            # reported `options: null`.
            if report.optionsRevealed is not None:
                captured = [Option(label=o, locator=None) for o in report.optionsRevealed]
            else:
                captured = control.options if control else None
            options, open_set = clean_options(captured)
            required = control.required if control else False
            conditional = self._conditional_for(control)

            question = self._build_question(
                qid=qid,
                stage_id=stage_id,
                label=label,
                control=control,
                report_value=report.valueUsed,
                constraint=report.constraint,
                options=options,
                open_set=open_set,
                required=required,
                conditional=conditional,
            )
            field = self._build_metadata_field(
                qid, report.locator, options, self._is_gate(control, options, open_set)
            )
            self._assert_no_credential_literal(question, field)
            block = self._script_block_for(question, field, report.intent, options)

            # All three exist. Commit them together.
            self._commit(question, field, block, stage, qid, is_new, required, options)
            self._assert_stages_agree()
            self._flush()
            self._record(ledger, "append", qid, started)
            return self.state()
        except Exception as exc:
            self._record(ledger, "append", stage_id, started, ok=False)
            log.error("generator append failed stage=%s: %s", stage_id, exc)
            raise

    def _commit(
        self,
        question: Question,
        field: MetadataField,
        block: str,
        stage: Stage,
        qid: str,
        is_new: bool,
        required: bool,
        options: list[Option] | None,
    ) -> None:
        """Attach the three staged writes to the documents.

        A fact captured a second time reconciles into the existing entry rather
        than appending a duplicate: requiredness is the OR across captures and
        the option list comes from the capture with the most real options, since
        taking either capture whole loses what the other had.
        """
        if not is_new:
            existing = next(q for q in self.questions_doc.questions if q.questionId == qid)
            merged_required, merged_options = merge_captures(
                existing.required,
                [Option(label=o, locator=None) for o in (existing.options or [])],
                required,
                options,
            )
            existing.required = merged_required
            if existing.type == "enum" and merged_options:
                existing.options = [o.label for o in merged_options]
            if question.exampleValue and self._published_walk is None:
                # Before any walk is published, the latest capture stands, which
                # is what a corrected fill needs: the correction replaces the
                # rejected value rather than both being kept. Once a walk is
                # published, `publish_walk` owns `exampleValue` and a later
                # walk's answer must not overwrite it.
                existing.exampleValue = question.exampleValue
            return

        self.questions_doc.questions.append(question)
        stage.fields.append(field)
        self.script_blocks.append(block)

    def _script_block_for(
        self, question: Question, field: MetadataField, intent: str, options: list[Option] | None
    ) -> str:
        """Emit the script's third recording of this control.

        Reads the client's answer by canonical. A hardcoded answer here would
        discard what the client said, which is the defect the arch doc names
        explicitly as `pickYesNo('No')`.
        """
        if field.options:
            walked = [(o.label, o.selector) for o in field.options if o.selector]
            return script_emitter.option_block(
                question.questionId, question.canonical, walked, question.required
            )
        if field.selectorYes or field.selectorNo:
            walked = [(lbl, sel) for lbl, sel in
                      (("Yes", field.selectorYes), ("No", field.selectorNo)) if sel]
            return script_emitter.option_block(
                question.questionId, question.canonical, walked, question.required
            )
        return script_emitter.fill_block(
            question.questionId,
            question.canonical,
            field.selector or "",
            question.required,
            intent,
        )

    def _conditional_for(self, control: Control | None) -> Conditional | None:
        """The parent gate and the revealing value, resolved to a questionId.

        `revealedBy` names a `fieldId`, which is per-page; the artifact's
        `conditional` names a `questionId`, so the mapping this class owns is
        what makes the reference stable.
        """
        if control is None or control.revealedBy is None:
            return None
        for (stage_id, field_id), qid in self._by_field.items():
            if field_id == control.revealedBy.fieldId:
                return Conditional(questionId=qid, value=control.revealedBy.equals)
        # The parent was never appended. Guessing a questionId would point the
        # script's branch at the wrong gate, so the reference is left unset.
        log.warning("no questionId for revealedBy fieldId=%s", control.revealedBy.fieldId)
        return None

    @staticmethod
    def _is_gate(control: Control | None, options: list[Option] | None, open_set: bool) -> bool:
        """Gate by shape, not by type name (arch doc 4)."""
        if open_set or control is None:
            return False
        if control.type == "toggle":
            return True
        return bool(options) and len(options) == 2

    # -- output -----------------------------------------------------------

    def _flush(self) -> None:
        """Write all three files. Incremental, so a crash leaves inspectable work."""
        self.questions_path.write_text(
            json.dumps(self.questions_doc.model_dump(exclude_none=True), indent=2)
        )
        self.metadata_path.write_text(
            json.dumps(self.metadata_doc.model_dump(exclude_none=True), indent=2)
        )
        self.script_path.write_text(
            script_emitter.header(
                self.carrier, self.business_type, self.insurance_type, self.script_name
            )
            + "".join(self.script_blocks)
            + script_emitter.footer()
        )

    def _record(
        self, ledger: RunLedger | None, action: str, detail: str, started: float, ok: bool = True
    ) -> None:
        """Record this step. The Generator is deterministic, so `usd` is always zero."""
        if ledger is None:
            return
        ledger.record(
            agent="generator",
            action=action,
            detail=detail,
            usd=0.0,
            ms=int((time.monotonic() - started) * 1000),
            ok=ok,
        )

    def publish_walk(self, walk: int) -> list[str]:
        """Fix one walk's answers as the `exampleValue` set, and return the ids set.

        Example values assembled per field across different walks are not a path
        the form ever rendered: q_010's answer survives from the walk where
        q_009 was Yes even after q_009 is set back to No, so replaying that set
        drives the form down a branch it never took. One walk's answers are a
        path that was actually walked.

        A question no walk answered -- one recorded from a blocked fill, or
        answered only in a walk that is not the published one -- keeps whatever
        it holds; the walk being published says nothing about it either way.
        """
        answers = self._answers.get(walk)
        if answers is None:
            raise ValueError(f"no walk {walk} to publish; walks recorded: {sorted(self._answers)}")

        self._published_walk = walk
        published = []
        for question in self.questions_doc.questions:
            value = answers.get(question.questionId)
            if value is None:
                continue
            if _SECRET_KEYS.search(question.canonical or "") and (
                value not in CREDENTIAL_PLACEHOLDERS
            ):
                # The same guard `append` applies. Publishing is a second write
                # path to `exampleValue`, and a rule enforced on only one of them
                # is not enforced.
                raise CredentialLeak(
                    f"{question.questionId} ({question.canonical}) carries a credential "
                    f"literal as exampleValue in walk {walk}; "
                    f"expected one of {CREDENTIAL_PLACEHOLDERS}"
                )
            question.exampleValue = value
            published.append(question.questionId)

        self._flush()
        log.info("published walk=%d questions=%d", walk, len(published))
        return published

    def state(self) -> GenerationState:
        """What has been written so far, so Loop can assert completion."""
        return GenerationState(
            questionIds=[q.questionId for q in self.questions_doc.questions],
            stages=[s.name for s in self.metadata_doc.stages],
            scriptSteps=len(self.script_blocks),
            unresolvedCanonicals=self._unresolved_canonicals(),
        )

    def _unresolved_canonicals(self) -> list[str]:
        """Canonical keys the script reads that no question supplies."""
        supplied = {q.canonical for q in self.questions_doc.questions}
        read = set(re.findall(r'requiredAnswer\(answers, "([^"]+)"', "".join(self.script_blocks)))
        read |= set(re.findall(r'optionalAnswer\(answers, "([^"]+)"', "".join(self.script_blocks)))
        return sorted(read - supplied)

    @property
    def minted_canonicals(self) -> dict[str, str]:
        """Names with no existing vocabulary entry, for `canonical_aliases` candidates."""
        return dict(self._resolver.minted)
