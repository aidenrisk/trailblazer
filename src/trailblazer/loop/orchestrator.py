

from typing import Any

from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.contracts import (
    Assignment,
    Diff,
    FrontierBoard,
    PageDescription,
    WalkSlice,
)


class Loop:
  

    def __init__(
        self,
        scraper: Any,
        frontier: FrontierAgent,
        formfiller: Any,
        replaygen: Any = None,
        validator: Any = None,
    ) -> None:
    
        self.scraper = scraper
        self.frontier = frontier
        self.formfiller = formfiller
        self.replaygen = replaygen
        self.validator = validator

    def fill_form(self, job: str, initial_page: PageDescription) -> WalkSlice:
        """
        Fill a form by orchestrating all agents.

        Args:
        - job: job ID (for logging/tracking)
        - initial_page: first PageDescription (page to start from)

        Returns:
        - WalkSlice: the successful walk, ready for ReplayGen
        """
        board: FrontierBoard | None = None
        current_page = initial_page

        # Main loop: fill the form until complete
        while True:
            # Step 1: Frontier analyzes the page and decides next action
            board, assignment = self.frontier.on_page_description(
                job, current_page, board
            )

            # Step 2: Check if we're done (submit or stop)
            if assignment.type in ("submit", "stop"):
                # Form is complete or blocked
                break

            # Step 3: FormFiller executes the assignment
            fill_report = self.formfiller.execute(job, current_page.stageId, assignment)

            # Step 4: Check if FormFiller succeeded
            if not fill_report.ok:
                # FormFiller failed, stop here
                break

            # Step 5: Scraper reads the page again (to see what changed)
            # TODO (v1): integrate with actual Scraper
            next_page = self._get_next_page_state(job, current_page, fill_report)

            # Step 6: Loop compares pages to build Diff
            diff = self._compare_pages(current_page, next_page)

            # Step 7: Frontier reacts to the diff
            board, action = self.frontier.on_diff(job, diff, assignment, board)

            # Step 8: Check if Frontier returned a WalkSlice (walk complete)
            if isinstance(action, list):
                # Walk is complete, return the slice
                return action

            # Step 9: Update current page for next iteration
            current_page = next_page

        # Return empty slice if we stopped before completing
        return []

    def _get_next_page_state(
        self, job: str, current_page: PageDescription, fill_report: Any
    ) -> PageDescription:
        """
        Get the page state after FormFiller executes.

        TODO (v1): Call actual Scraper to re-read the page.
        For now, this is a placeholder.
        """
        # In a real system: return self.scraper.read_page(job)
        # For now, assume FormFiller returns it or we use a mock
        return current_page

    def _compare_pages(
        self, page_before: PageDescription, page_after: PageDescription
    ) -> Diff:
        """
        Compare two pages to see what changed.

        TODO (v1): Real diff logic (compare controls, detect added/removed/changed).
        For now, simple heuristic: more controls = +ve, same = -ve.
        """
        if len(page_after.controls) > len(page_before.controls):
            return Diff(polarity="+ve")
        return Diff(polarity="-ve")
