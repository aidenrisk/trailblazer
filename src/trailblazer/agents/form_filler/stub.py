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
                        # A discovered choice may have no node of its own (a
                        # native <select>), so fall back to the control we were
                        # asked to fill. FillStep.locator is not optional.
                        fieldId=field_id,
                        action="select",
                        locator=chosen.locator or assignment.locator,
                        value=chosen.label,
                    )
                ],
                landed=[field_id],
                fieldId=field_id,
                discoveredOptions=list(options),
                chosenOption=chosen.label,
            )

        logger.info("[%s] typed %r into %s", job, assignment.value, field_id)
        return FillReport(
            ok=True,
            steps=[
                FillStep(
                    fieldId=field_id,
                    action="fill",
                    locator=assignment.locator,
                    value=assignment.value,
                )
            ],
            landed=[field_id],
            fieldId=field_id,
            discoveredOptions=None,  # not a chooser
        )

    def _set_option(self, job: str, assignment: SetOptionAssignment) -> FillReport:
        """Apply the option-locator rule the contract states.

        `locator` set  -> the choice is its own node: click it.
        `locator` None -> no node of its own: select_option(label) on the parent.

        A real FormFiller calls `page.click(locator)` in the first case and
        `page.select_option(controlLocator, label=option)` in the second.
        Clicking an `<option>` node does not work, which is why the None is
        carried all the way here rather than collapsed upstream.
        """
        if assignment.locator is not None:
            action, target = "click", assignment.locator
        else:
            action, target = "select", assignment.controlLocator

        logger.info(
            "[%s] %s %s=%s via %s",
            job,
            action,
            assignment.fieldId,
            assignment.option,
            target,
        )
        return FillReport(
            ok=True,
            steps=[
                FillStep(
                    fieldId=assignment.fieldId,
                    action=action,
                    locator=target,
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
