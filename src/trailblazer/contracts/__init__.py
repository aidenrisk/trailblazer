"""Data contracts between pipeline components. Re-exports only.

`page_description` is what the Scraper sees. `scraper_result` is the Scraper's
internal perceive result. `walk` is everything that happens to a page: Frontier's
board, Assignments, FillReports, the Diff, and the Walk ReplayGen compiles.
"""

from trailblazer.contracts.page_description import (
    Control,
    ControlType,
    Option,
    PageDescription,
    RevealedBy,
)
from trailblazer.contracts.scraper_result import PerceiveRequest, ScraperResult
from trailblazer.contracts.walk import (
    Assignment,
    ChangedControl,
    ControlState,
    Diff,
    ErrorClass,
    FillFieldAssignment,
    FillReport,
    FillStep,
    FrontierBoard,
    LastPageProbeAssignment,
    SetOptionAssignment,
    SimpleAssignment,
    Walk,
    WalkPath,
    WalkSlice,
    WalkStep,
)

__all__ = [
    # Scraper output
    "Control",
    "ControlType",
    "Option",
    "PageDescription",
    "RevealedBy",
    "PerceiveRequest",
    "ScraperResult",
    # Frontier memory
    "ControlState",
    "FrontierBoard",
    # Frontier -> FormFiller
    "Assignment",
    "FillFieldAssignment",
    "LastPageProbeAssignment",
    "SetOptionAssignment",
    "SimpleAssignment",
    # FormFiller -> Loop -> Frontier
    "ErrorClass",
    "FillReport",
    "FillStep",
    # Scraper -> Frontier
    "ChangedControl",
    "Diff",
    # Frontier -> ReplayGen
    "Walk",
    "WalkPath",
    "WalkSlice",
    "WalkStep",
]
