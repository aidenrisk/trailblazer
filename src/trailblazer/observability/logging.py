"""Logging setup. Stdlib `logging`, one formatter, no framework.

Every line carries a wall-clock time, a level, and then the event body written
by `observability.events`: an agent, an action from a closed vocabulary, and
`key=value` fields. A run is read by grepping an action or a fieldId, so a line
that spends its width on a module path or on prose is a line that cannot be
counted.

Never log an API key or full page content: the payload is logged as a count and
a byte size only. Contract data (PageDescription, FillReport) is structured and
is logged in full at DEBUG.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)-5s %(message)s"
_DATEFMT = "%H:%M:%S"


def configure_logging(level: str = "INFO", to_file: Path | None = None) -> None:
    """Attach a stderr handler, and a file handler when `to_file` is given.

    Only our own logger is touched, so importing this never reconfigures a host
    application's root logging (FastAPI/uvicorn keep their own handlers).

    `to_file` is the run's own log, written beside its artifacts. A crawl is
    diagnosed from the log after the browser is gone, and a shared path in
    `/tmp` was overwritten by the next launch: three runs' evidence was lost
    that way before the per-run file existed.
    """
    global _CONFIGURED
    logger = logging.getLogger("trailblazer")
    logger.setLevel(level.upper())
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
        _CONFIGURED = True

    if to_file is not None:
        to_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(to_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


def log_contract(logger: logging.Logger, name: str, payload: Any) -> None:
    """Log one contract object as JSON at DEBUG.

    Contracts are the debugging surface: a pipeline failure is diagnosed from
    what each agent handed the next, so each is recorded whole.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    dump = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    logger.debug("%s %s", name, json.dumps(dump, separators=(",", ":")))


def get_logger(name: str) -> logging.Logger:
    """A child of the `trailblazer` logger, named for the calling module."""
    return logging.getLogger(f"trailblazer.{name.removeprefix('trailblazer.')}")
