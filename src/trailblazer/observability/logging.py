"""Logging setup. Stdlib `logging`, one formatter, no framework.

Lines are `key=value` so they read as prose and grep as fields. `job_id` is
attached where the caller knows it, by passing it into the message rather than
through a context system -- the call sites that have it are few enough that a
context propagation layer would cost more than it saves.

Never log an API key or full page content: the payload is logged as a count and
a byte size only. Contract data (PageDescription, FillReport) is structured and
is logged in full at DEBUG.
"""

import json
import logging
import sys
from typing import Any

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Attach one stderr handler to the `trailblazer` logger. Idempotent.

    Only our own logger is touched, so importing this never reconfigures a host
    application's root logging (FastAPI/uvicorn keep their own handlers).
    """
    global _CONFIGURED
    logger = logging.getLogger("trailblazer")
    logger.setLevel(level.upper())
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    _CONFIGURED = True


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
