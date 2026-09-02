"""
StubScraper: stands in for the real Scraper while it's built separately.

No Playwright, no CDP, no browser. It replays a scripted list of pages and
simulates the two things that actually matter to Frontier:

1. Fills persist. It keeps a mutable working copy of the current page, so an
   option list learned on one look is still there on the next — exactly like a
   live tab.
2. Choices reveal fields. A `reveals` map says "picking option X of control Y
   makes these controls appear", which is how the revealed-field path
   (Frontier must explore them before finishing the page) gets exercised.

It returns (PageDescription, Diff) together — the Diff comes from the Scraper,
so Loop doesn't diff anything itself.
"""

import logging
from typing import Literal

from trailblazer.contracts import (
    Assignment,
    ChangedControl,
    Control,
    Diff,
    FillFieldAssignment,
    FillReport,
    PageDescription,
    SetOptionAssignment,
    SimpleAssignment,
)

logger = logging.getLogger(__name__)


class StubScraper:
    """
    Args:
    - pages: the scripted page sequence. A `next` assignment advances to the
             following one; the last page should have next=None so the walk ends.
    - reveals: {(fieldId, optionLabel): [Controls that appear]}. Applied once,
               the first time that option is chosen.
    """

    def __init__(
        self,
        pages: list[PageDescription],
        reveals: dict[tuple[str, str], list[Control]] | None = None,
    ) -> None:
        if not pages:
            raise ValueError("StubScraper needs at least one page")
        self.pages = pages
        self.reveals = reveals or {}
        self.index = 0
        # Deep copy so mutations (discovered options, revealed fields) don't
        # corrupt the caller's fixtures — tests reuse these dicts.
        self.current = pages[0].model_copy(deep=True)
        self._fired: set[tuple[str, str]] = set()

    def look(
        self,
        job: str,
        objective: Literal["perceive", "post_fill"] = "perceive",
        last_assignment: Assignment | None = None,
        fill_report: FillReport | None = None,
    ) -> tuple[PageDescription, Diff]:
        """
        Read the page as it is now, and say what changed.

        Returns: (PageDescription, Diff)
        """
        # First look at a fresh walk: nothing has happened yet.
        if last_assignment is None:
            return (self.current.model_copy(deep=True), Diff(polarity="-ve"))

        if isinstance(last_assignment, SimpleAssignment) and last_assignment.type == "next":
            return self._advance(job)

        added: list[Control] = []

        # The filler discovered a control was really a chooser — a real Scraper
        # would now see the expanded widget's options in the DOM, so persist
        # them onto the working copy.
        if (
            isinstance(last_assignment, FillFieldAssignment)
            and fill_report is not None
            and fill_report.discoveredOptions is not None
        ):
            for control in self.current.controls:
                if control.fieldId == last_assignment.fieldId:
                    control.options = list(fill_report.discoveredOptions)
                    logger.info(
                        "[%s] scraper now sees options on %s: %s",
                        job,
                        control.fieldId,
                        [o.label for o in control.options],
                    )
                    break

        # Choosing an option can reveal new fields. An option picked during
        # discovery counts: the filler selected it just as surely as it would
        # have for an explicit set_option, so the page reacts the same way.
        key = self._chosen_option(last_assignment, fill_report)
        if key is not None and key in self.reveals and key not in self._fired:
            self._fired.add(key)
            added = [c.model_copy(deep=True) for c in self.reveals[key]]
            self.current.controls.extend(added)
            logger.info("[%s] %s=%s revealed %s", job, key[0], key[1], [c.fieldId for c in added])

        diff = Diff(
            polarity="+ve" if added else "-ve",
            addedControls=[_ref(c) for c in added],
        )
        return (self.current.model_copy(deep=True), diff)

    @staticmethod
    def _chosen_option(
        last_assignment: Assignment | None, fill_report: FillReport | None
    ) -> tuple[str, str] | None:
        """Which (fieldId, optionLabel) did the filler just select, if any?"""
        if isinstance(last_assignment, SetOptionAssignment):
            return (last_assignment.fieldId, last_assignment.option)
        if (
            isinstance(last_assignment, FillFieldAssignment)
            and fill_report is not None
            and fill_report.chosenOption is not None
        ):
            return (last_assignment.fieldId, fill_report.chosenOption)
        return None

    def _advance(self, job: str) -> tuple[PageDescription, Diff]:
        """Clicking Next moves to the next scripted page."""
        if self.index + 1 >= len(self.pages):
            # No page scripted after this one. Report the page settled rather
            # than pretending to navigate.
            logger.warning("[%s] next clicked but no further page scripted", job)
            return (self.current.model_copy(deep=True), Diff(polarity="-ve"))

        self.index += 1
        self.current = self.pages[self.index].model_copy(deep=True)
        logger.info("[%s] advanced to %s", job, self.current.stageId)
        return (
            self.current.model_copy(deep=True),
            Diff(
                polarity="+ve",
                addedControls=[_ref(c) for c in self.current.controls],
            ),
        )


def _ref(control: Control) -> ChangedControl:
    return ChangedControl(
        label=control.label, locator=control.locator, fieldId=control.fieldId
    )
