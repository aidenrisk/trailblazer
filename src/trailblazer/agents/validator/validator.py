"""Run a generated replay script and report what happened.

The Validator does not repair a script and does not drive the browser -- the
script does that. Its whole job is to invoke the script the way the replay
runner does, then read the outcome out of whatever the script managed to leave
behind, without guessing.

Reading the outcome is the substance. Section 6 requires the script to write
`last-run.json` beside itself *and* print one `RRSTATUS <json>` line. Both can
be missing: a script that throws before its handler runs leaves neither. So the
sources are tried in the contract's order -- file, then stdout line, then log
text -- and the one that answered is reported, because "the file said quote"
and "we inferred quote from a log line" are different amounts of evidence.

A decline is a SUCCESSFUL run. `appetite-decline` means the carrier refused the
risk on eligibility, which is the correct answer to a question that was asked
properly. It is never a defect to route around.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from trailblazer.agents.validator.static_checks import static_checks
from trailblazer.contracts.validation import ValidationRequest, ValidationResult
from trailblazer.observability.ledger import RunLedger
from trailblazer.observability.logging import get_logger, log_contract
from trailblazer.shared.config import Settings

log = get_logger(__name__)

__all__ = ["validate", "static_checks", "read_outcome", "normalize_outcome"]

RRSTATUS_LINE = re.compile(r"^\s*RRSTATUS\s+(\{.*\})\s*$", re.MULTILINE)
"""The stdout fallback. Anchored per line so log noise around it is ignored."""

_LOG_TEXT_OUTCOME = re.compile(
    r"\b(quote|appetite[-_ ]decline|stuck)\b", re.IGNORECASE
)
"""Last-resort scrape. Only consulted when neither status channel produced JSON."""

_NODE_ERROR_LINE = re.compile(
    r"^(?:Uncaught\s+)?(?:\w*Error|\w*Exception)\b.*$", re.MULTILINE
)
"""Node's `<ErrorName>: <message>` line, the one that names the failure."""

_NODE_NOISE = re.compile(r"^(?:at\s|Node\.js\s+v|\^+$)")
"""Stack frames, the caret marker and the version banner. All content-free."""

_DEFAULT_TIMEOUT_S = 900
"""A replay walks a whole portal flow; minutes are normal, a quarter hour is not."""

_SUCCESS_OUTCOMES = frozenset({"quote", "appetite-decline"})


def normalize_outcome(raw: Any, reached_quote: bool) -> str:
    """Map a reported outcome onto the closed vocabulary.

    `appetite_decline` and `appetite decline` are the same result spelled
    differently by different RoadRunner versions. Anything still unrecognized
    collapses to `quote` when the run reached one and `stuck` otherwise, so an
    unknown string never becomes a fourth outcome.
    """
    if isinstance(raw, str):
        cleaned = raw.strip().lower().replace("_", "-").replace(" ", "-")
        if cleaned in ("quote", "stuck"):
            return cleaned
        if cleaned == "appetite-decline":
            return "appetite-decline"
    return "quote" if reached_quote else "stuck"


def _coerce_float(value: Any) -> float | None:
    """A premium the script wrote as `"3,036"` or `"$3036"` is still a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = re.sub(r"[^\d.\-]", "", value)
        if stripped not in ("", "-", ".", "-."):
            return float(stripped)
    return None


def _coerce_str(value: Any) -> str | None:
    """Keep only real text. A script writing `false` for a URL means absent."""
    return value if isinstance(value, str) and value.strip() else None


def _status_from_file(script_path: Path) -> dict[str, Any] | None:
    """`last-run.json` beside the script. The preferred source."""
    path = script_path.parent / "last-run.json"
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("last-run.json present but unreadable path=%s err=%s", path, exc)
        return None
    return loaded if isinstance(loaded, dict) else None


def _status_from_stdout(stdout: str) -> dict[str, Any] | None:
    """The last `RRSTATUS <json>` line. Last, so a retry supersedes its first try."""
    for match in reversed(list(RRSTATUS_LINE.finditer(stdout))):
        try:
            loaded = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def _status_from_log_text(text: str) -> dict[str, Any] | None:
    """Scrape an outcome word out of raw logs. Carries no premium or quote data."""
    matches = _LOG_TEXT_OUTCOME.findall(text)
    if not matches:
        return None
    word = matches[-1].strip().lower().replace("_", "-").replace(" ", "-")
    return {"outcome": word, "reachedQuote": word == "quote"}


def read_outcome(
    script_path: Path, stdout: str = "", stderr: str = ""
) -> tuple[dict[str, Any] | None, str]:
    """Find the run's status object and say which channel produced it.

    Returns `(status, source)` where source is `last-run.json`, `RRSTATUS`,
    `log-text` or `none`. The source is reported rather than discarded: it is
    the difference between a status the script asserted and one we inferred.
    """
    status = _status_from_file(script_path)
    if status is not None:
        return status, "last-run.json"

    status = _status_from_stdout(stdout)
    if status is not None:
        return status, "RRSTATUS"

    status = _status_from_log_text(f"{stdout}\n{stderr}")
    if status is not None:
        return status, "log-text"

    return None, "none"


def _result_from_status(status: dict[str, Any]) -> ValidationResult:
    """Build the contract object from a status the script reported."""
    reached_quote = bool(status.get("reachedQuote"))
    outcome = normalize_outcome(status.get("outcome"), reached_quote)

    # The vocabulary is closed and `reachedQuote` is derived data, so a status
    # claiming `quote` without the flag still reached one.
    if outcome == "quote":
        reached_quote = True

    return ValidationResult(
        outcome=outcome,
        reachedQuote=reached_quote,
        stoppedReason=_coerce_str(status.get("stoppedReason")),
        premium=_coerce_float(status.get("premium")),
        premiumDisplay=_coerce_str(status.get("premiumDisplay")),
        quoteNumber=_coerce_str(status.get("quoteNumber")),
        quoteUrl=_coerce_str(status.get("quoteUrl")),
        bindUrl=_coerce_str(status.get("bindUrl")),
        bindControlLabel=_coerce_str(status.get("bindControlLabel")),
        exitCode=0 if outcome in _SUCCESS_OUTCOMES else 1,
    )


def _stuck(reason: str) -> ValidationResult:
    """A run that produced no status at all."""
    return ValidationResult(
        outcome="stuck", reachedQuote=False, stoppedReason=reason, exitCode=1
    )


def _clear_stale_status(script_path: Path) -> None:
    """Delete a previous run's `last-run.json`.

    Without this the preferred source outlives the run that wrote it, and a
    script that crashes before writing reports the last run's quote.
    """
    stale = script_path.parent / "last-run.json"
    if stale.is_file():
        stale.unlink()


def validate(
    request: ValidationRequest,
    settings: Settings,
    config_path: str | Path | None = None,
    ledger: RunLedger | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> ValidationResult:
    """Run one replay script against one set of answers and report the outcome.

    Static checks run first and are logged as warnings only -- section 6 keeps
    the flow draft rather than rejecting it, so a violation never stops the run.

    `config_path` is the four-key creds file passed through as `--config`.
    """
    script_path = Path(request.script_path).resolve()
    started = time.monotonic()

    if not script_path.is_file():
        raise FileNotFoundError(f"replay script not found: {script_path}")

    warnings = static_checks(script_path)
    for warning in warnings:
        log.warning("static check job_id=%s script=%s %s", request.job_id, script_path.name, warning)
    if ledger is not None:
        ledger.record(
            agent="validator",
            action="static_checks",
            detail=f"{script_path.name} warnings={len(warnings)}",
            ms=int((time.monotonic() - started) * 1000),
            ok=not warnings,
        )

    _clear_stale_status(script_path)

    # Both paths are resolved because the script runs with `cwd` set to its own
    # directory, where a caller's relative path would no longer point anywhere.
    command = ["node", str(script_path), str(Path(request.answers_path).resolve())]
    if config_path is not None:
        command += ["--config", str(Path(config_path).resolve())]

    env_headless = "false" if (request.headed or settings.headed) else "true"
    run_started = time.monotonic()
    log.info(
        "replay start job_id=%s script=%s answers=%s headless=%s",
        request.job_id,
        script_path,
        request.answers_path,
        env_headless,
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=script_path.parent,
            env={**_base_env(), "HEADLESS": env_headless},
        )
        stdout, stderr, process_exit = completed.stdout, completed.stderr, completed.returncode
        crash_reason = None
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        process_exit = 1
        crash_reason = f"replay exceeded {timeout_s}s"

    status, source = read_outcome(script_path, stdout, stderr)

    if status is not None:
        result = _result_from_status(status)
    else:
        # No status on any channel: the script died before its handler ran.
        # The crash message is the only account of why, so it becomes the reason.
        result = _stuck(crash_reason or _crash_message(stderr, stdout, process_exit))

    elapsed_ms = int((time.monotonic() - run_started) * 1000)
    log.info(
        "replay end job_id=%s outcome=%s success=%s source=%s exit=%d process_exit=%d ms=%d",
        request.job_id,
        result.outcome,
        result.success,
        source,
        result.exitCode,
        process_exit,
        elapsed_ms,
    )
    log_contract(log, "ValidationResult", result)

    if ledger is not None:
        ledger.record(
            agent="validator",
            action="validate",
            detail=f"{result.outcome} via {source}",
            ms=elapsed_ms,
            ok=result.success,
        )

    return result


def _crash_message(stderr: str, stdout: str, process_exit: int) -> str:
    """The most specific account of a crash the process left behind.

    Node prints an uncaught throw as `<ErrorName>: <message>`, then a stack,
    then a `Node.js vX` banner. The banner and the stack frames are the last
    lines but say nothing about the failure, so the error line is matched
    directly and only then does the last line stand in for it.
    """
    for stream in (stderr, stdout):
        match = _NODE_ERROR_LINE.search(stream)
        if match:
            return match.group(0).strip()
    for stream in (stderr, stdout):
        lines = [
            line.strip()
            for line in stream.splitlines()
            if line.strip() and not _NODE_NOISE.match(line.strip())
        ]
        if lines:
            return lines[-1]
    return f"replay exited {process_exit} without writing a status"


def _base_env() -> dict[str, str]:
    """The parent environment, so RR_* proxy and browser settings pass through."""
    return dict(os.environ)
