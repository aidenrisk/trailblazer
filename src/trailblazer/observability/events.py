"""One structured line per thing the crawl does.

A crawl is diagnosed after the fact from its log, and prose does not survive
that: "reopening q_016 on its rejection" reads well and answers none of the
questions asked of it -- which locator, which value, how many times before,
what the page said back. Every action is written here in one shape instead:

    HH:MM:SS LEVEL agent action key=value key=value

`action` comes from a fixed vocabulary (`_ACTIONS`), so a run can be counted
and grouped without parsing sentences. Values are rendered by `_fmt`: quoted
when they carry spaces, truncated at `_MAX_VALUE`, and never wrapped, so one
event is always one line and `grep` reaches all of it.

Only stable facts go in a value. Anything that would differ between two runs of
the same page -- an elapsed time, a cost -- is a value too, but named, so it can
be summed rather than read.
"""

import logging
import time
from typing import Any

from trailblazer.observability.logging import get_logger

log = get_logger("event")

_MAX_VALUE = 220
"""Characters of one value. A locator or a page message past this is cut with a
marker; the whole of it is in the contract dump at DEBUG."""

_ACTIONS = frozenset({
    "run", "login", "look", "assign", "fill", "advance", "dismiss",
    "vision", "restart", "reopen", "stuck", "generate", "validate", "summary",
})
"""What a crawl can do. A new kind of step is added here deliberately, so the
vocabulary a reader greps for stays closed."""


def _fmt(value: Any) -> str:
    """One value, rendered so the line stays one line and parses back."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6f}"
    text = str(value).replace("\n", "\\n").replace("\r", "")
    if len(text) > _MAX_VALUE:
        text = text[:_MAX_VALUE] + f"…+{len(str(value)) - _MAX_VALUE}"
    if " " in text or "=" in text or '"' in text or not text:
        # A locator carries quotes of its own -- `a[href="/x"]` -- and an
        # unescaped one ended the value early and left the rest looking like
        # fields.
        return '"' + text.replace('"', '\\"') + '"'
    return text


def event(action: str, agent: str, level: int = logging.INFO, **fields: Any) -> None:
    """Write one event. `action` must be in `_ACTIONS`; unknown ones raise.

    Raising rather than accepting anything keeps the vocabulary closed: a typo
    would otherwise produce a line no reader greps for and no summary counts.
    """
    if action not in _ACTIONS:
        raise ValueError(f"unknown action {action!r}; add it to _ACTIONS deliberately")
    body = " ".join(f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None)
    log.log(level, "%-8s %-10s %s", agent, action, body)


class Timed:
    """Time a block and write its event on exit, with `ms` and any late fields.

    Used where the duration is the point -- a look, a fill, a press. Fields
    known up front are given to the constructor; anything learned while the
    block runs is set on the object and appears in the same line, so one action
    is one event rather than a start line and an end line to pair up.
    """

    def __init__(self, action: str, agent: str, **fields: Any) -> None:
        self.action = action
        self.agent = agent
        self.fields = fields
        self.level = logging.INFO
        self._started = 0.0

    def __enter__(self) -> "Timed":
        self._started = time.monotonic()
        return self

    def set(self, **fields: Any) -> "Timed":
        """Add or replace fields to be written when the block ends."""
        self.fields.update(fields)
        return self

    def fail(self, **fields: Any) -> "Timed":
        """Mark the event as a failure and add fields describing it."""
        self.level = logging.WARNING
        self.fields.update(fields)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        ms = int((time.monotonic() - self._started) * 1000)
        if exc is not None:
            event(
                self.action, self.agent, logging.ERROR,
                ms=ms, error=type(exc).__name__, detail=str(exc), **self.fields,
            )
            return
        event(self.action, self.agent, self.level, ms=ms, **self.fields)
