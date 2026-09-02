"""What Loop hands the scraper, and what the scraper hands back.

The scraper owns the diff (supersedes MASTER.md, which put it in Loop): it
receives the prior `PageDescription`, perceives the current page, and returns
both the new description and the polarity Loop routes on.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from trailblazer.contracts.page_description import PageDescription


class PerceiveRequest(BaseModel):
    """One look at one page. The scraper holds no state between these."""

    model_config = ConfigDict(populate_by_name=True)

    job_id: str
    page_index: int
    """Loop owns this counter; it alone knows if the walk is advancing or backtracking."""

    objective: str | None = None
    prior: PageDescription | None = None
    """The previous look at this page. `None` on a first perceive."""

    assignment: dict[str, str] | None = None
    """fieldId -> value just submitted, used to populate `revealedBy` on new controls."""


class ScraperResult(BaseModel):
    """The new page description plus the comparison against the prior one."""

    model_config = ConfigDict(populate_by_name=True)

    page: PageDescription

    polarity: Literal["+ve", "-ve"]
    """`+ve` = the page changed, keep walking. `-ve` = settled, go to replay gen."""

    addedControls: list[str]
    """fieldIds (in the new page) whose locators were absent from the prior."""

    removedControls: list[str]
    """fieldIds (in the prior page) whose locators are absent from the new one."""

    changedControls: list[str]
    """fieldIds present in both whose describable properties differ."""
