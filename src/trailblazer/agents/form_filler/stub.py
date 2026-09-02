"""
StubFormFiller: stands in for the real FormFiller (Playwright) while it's built.

The one behaviour worth simulating faithfully is discovery: a control Scraper
reported as a plain field turns out, when you try to fill it, to be a dropdown.
The real FormFiller learns this from the DOM; the stub learns it from the
`discoverable` map. Either way it reports the options back on the FillReport, and
Loop hands that to Frontier.
"""

import logging

from trailblazer.contracts import (
    Assignment,
    FillFieldAssignment,
    FillReport,
    FillStep,
    Option,
    SetOptionAssignment,
    SimpleAssignment,
)

logger = logging.getLogger(__name__)


class StubFormFiller:
    """
    Args:
    - discoverable: {fieldId: [Option, ...]} — controls that are secretly
                    choosers. On the first fill_field for one of these, the
                    stub reports the options and picks the first.
    """

    def __init__(self, discoverable: dict[str, list[Option]] | None = None) -> None:
        self.discoverable = discoverable or {}
        self._discovered: set[str] = set()

    def execute(self, job: str, stage_id: str, assignment: Assignment) -> FillReport:
        if isinstance(assignment, FillFieldAssignment):
            return self._fill_field(job, assignment)
        if isinstance(assignment, SetOptionAssignment):
            return self._set_option(job, assignment)
        if isinstance(assignment, SimpleAssignment):
            return self._navigate(job, assignment)

        logger.warning("[%s] stub filler got %s", job, assignment.type)
        return FillReport(ok=False, errorClass="widget")

    def _fill_field(self, job: str, assignment: FillFieldAssignment) -> FillReport:
        field_id = assignment.fieldId
        options = self.discoverable.get(field_id)

        # Report the discovery only once. On a later fill of the same control
        # Frontier already knows the options, so re-reporting would reset its
        # walked/pending progress.
        if options and field_id not in self._discovered:
            self._discovered.add(field_id)
            chosen = options[0]
            logger.info(
                "[%s] %s is a dropdown: %s — picking %s",
                job,
                field_id,
                [o.label for o in options],
                chosen.label,
            )
            return FillReport(
                ok=True,
                steps=[
                    FillStep(
                        fieldId=field_id,
                        action="select",
                        locator=chosen.locator,
                        value=chosen.label,
                    )
                ],
                landed=[field_id],
                fieldId=field_id,
                discoveredOptions=list(options),
                chosenOption=chosen.label,
            )

        # A credential fill names a key, never a value. The stub records the key
        # as what it "typed", which is also what the real filler's report must
        # show: the secret itself never appears in a FillStep.
        typed = assignment.value if assignment.value is not None else assignment.credentialKey
        logger.info("[%s] typed %r into %s", job, typed, field_id)
        return FillReport(
            ok=True,
            steps=[
                FillStep(
                    fieldId=field_id,
                    action="fill",
                    locator=assignment.locator,
                    value=typed,
                )
            ],
            landed=[field_id],
            fieldId=field_id,
            discoveredOptions=None,  # not a chooser
        )

    def _set_option(self, job: str, assignment: SetOptionAssignment) -> FillReport:
        # Acts on assignment.locator — the option's own locator, not the
        # parent control's. That's the contract.
        logger.info(
            "[%s] selected %s=%s via %s",
            job,
            assignment.fieldId,
            assignment.option,
            assignment.locator,
        )
        return FillReport(
            ok=True,
            steps=[
                FillStep(
                    fieldId=assignment.fieldId,
                    action="select",
                    locator=assignment.locator,
                    value=assignment.option,
                )
            ],
            landed=[assignment.fieldId],
            fieldId=assignment.fieldId,
            chosenOption=assignment.option,
        )

    def _navigate(self, job: str, assignment: SimpleAssignment) -> FillReport:
        logger.info("[%s] %s", job, assignment.type)
        return FillReport(
            ok=True,
            steps=[FillStep(action="click", locator=f"<{assignment.type}>")],
            advance=assignment.type in ("next", "back", "submit"),
        )
