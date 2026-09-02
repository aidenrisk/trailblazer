"""Logging setup. Stdlib `logging`, one formatter, no framework.

Lines are `key=value` so they read as prose and grep as fields. `job_id` is
attached where the caller knows it, by passing it into the message rather than
through a context system -- the call sites that have it are few enough that a
context propagation layer would cost more than it saves.

Never log an API key or full page content: the payload is logged as a count and
a byte size only.
"""

import logging
import sys

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


def get_logger(name: str) -> logging.Logger:
    """A child of the `trailblazer` logger, named for the calling module."""
    return logging.getLogger(f"trailblazer.{name.removeprefix('trailblazer.')}")
