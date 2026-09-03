"""The one browser every agent shares, and the file that advertises it.

`launch` starts a headed Chromium on a persistent profile and records its CDP
endpoint here. Agents read the record instead of being told a port, so a human
logs in once and every later run -- scraper, form filler, validator -- drives
that same authenticated browser.
"""

import json
import os
import time
from pathlib import Path

from trailblazer.agents.browser.session import devtools_running
from trailblazer.observability.logging import get_logger

log = get_logger(__name__)


def session_path(configured: str) -> Path:
    """Absolute path of the session record."""
    return Path(os.path.expanduser(configured))


def write_record(configured: str, cdp_port: int, profile_dir: str) -> Path:
    """Record the live endpoint for other agents to read."""
    path = session_path(configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cdpPort": cdp_port,
                "cdpEndpoint": f"http://127.0.0.1:{cdp_port}",
                "profileDir": profile_dir,
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            indent=2,
        )
    )
    return path


def read_record(configured: str) -> dict | None:
    """The recorded endpoint, or None when absent or unreadable."""
    path = session_path(configured)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def live_port(configured: str, fallback: int) -> int:
    """The port to attach to: the recorded one when it answers, else `fallback`.

    A stale record outlives the browser it described, so the record is trusted
    only while a DevTools server is still answering on it.
    """
    record = read_record(configured)
    if record:
        port = record.get("cdpPort")
        if isinstance(port, int) and devtools_running(port):
            return port
        log.debug("session record at %s is stale", session_path(configured))
    return fallback


def clear_record(configured: str) -> None:
    """Remove the record. Called when a launch finds its port unusable."""
    session_path(configured).unlink(missing_ok=True)
