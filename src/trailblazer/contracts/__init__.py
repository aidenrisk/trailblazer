"""Data contracts between pipeline components. Re-exports only.

    page_description   what the scraper sees        (scraper)
    scraper_result     that plus the diff           (scraper -> loop -> frontier)
    frontier_board     exploration state            (frontier, internal)
    assignment         one action to perform        (frontier -> form filler)
    fill_report        what was done and learned    (form filler -> loop -> frontier)
    walk               replayable paths             (frontier -> replay gen)

The scraper owns the diff, so there is no separate `Diff` model: polarity and
the added/removed/changed fieldIds ride on `ScraperResult` alongside the page.
"""

from trailblazer.contracts.assignment import (
    Assignment,
    FillFieldAssignment,
    LastPageProbeAssignment,
    SetOptionAssignment,
    SimpleAssignment,
)
from trailblazer.contracts.fill_report import FillReport, FillStep
from trailblazer.contracts.frontier_board import ControlState, FrontierBoard
from trailblazer.contracts.page_description import (
    Control,
    ControlType,
    Option,
    PageDescription,
    RevealedBy,
)
from trailblazer.contracts.scraper_result import PerceiveRequest, ScraperResult
from trailblazer.contracts.walk import Walk, WalkPath, WalkSlice, WalkStep

__all__ = [
    # scraper output
    "Control",
    "ControlType",
    "Option",
    "PageDescription",
    "RevealedBy",
    "PerceiveRequest",
    "ScraperResult",
    # frontier memory
    "ControlState",
    "FrontierBoard",
    # frontier -> form filler
    "Assignment",
    "FillFieldAssignment",
    "LastPageProbeAssignment",
    "SetOptionAssignment",
    "SimpleAssignment",
    # form filler -> loop -> frontier
    "FillReport",
    "FillStep",
    # frontier -> replay gen
    "Walk",
    "WalkPath",
    "WalkSlice",
    "WalkStep",
]
