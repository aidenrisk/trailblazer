"""The retry around model calls: what is asked again, what is not, and when it stops.

No provider is contacted. The callable under test is a fake that fails on a
schedule, and the backoff sleep is patched out.
"""

import httpx
import pytest

from trailblazer.shared import models
from trailblazer.shared.models import TransientModelError, invoke_with_retry


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(models.time, "sleep", lambda s: None)


def _failing(errors: list[BaseException], then: str = "ok"):
    """A callable that raises each error in turn, then returns `then`."""
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if errors:
            raise errors.pop(0)
        return then

    return call, calls


def test_a_dropped_connection_is_retried_and_the_answer_returned() -> None:
    """One dropped connection ended a live run five restarts deep."""
    call, calls = _failing([httpx.RemoteProtocolError("peer closed connection")])

    assert invoke_with_retry(call, step="t") == "ok"
    assert calls["n"] == 2


def test_a_provider_5xx_reported_as_a_value_error_is_retried() -> None:
    """The OpenRouter client raises ValueError with `(code: N)` for a server-side failure."""
    call, calls = _failing([
        ValueError("OpenRouter API returned an error during streaming: overloaded (code: 502)"),
        ValueError("OpenRouter API returned an error during streaming: rate limited (code: 429)"),
    ])

    assert invoke_with_retry(call, step="t") == "ok"
    assert calls["n"] == 3


def test_a_400_class_error_is_not_retried() -> None:
    """A malformed request does not become well-formed by asking again."""
    call, calls = _failing([ValueError("bad request: unknown parameter (code: 400)")])

    with pytest.raises(ValueError, match="code: 400"):
        invoke_with_retry(call, step="t")
    assert calls["n"] == 1


def test_an_unrelated_error_is_raised_at_once() -> None:
    call, calls = _failing([KeyError("schema")])

    with pytest.raises(KeyError):
        invoke_with_retry(call, step="t")
    assert calls["n"] == 1


def test_an_empty_reply_is_asked_again() -> None:
    call, calls = _failing([TransientModelError("no value")])

    assert invoke_with_retry(call, step="t") == "ok"
    assert calls["n"] == 2


def test_after_the_last_attempt_the_last_error_is_raised_not_swallowed() -> None:
    """Three failures is a real outage; the cause still has to reach the log."""
    call, calls = _failing([
        httpx.ReadTimeout("t1"), httpx.ReadTimeout("t2"), httpx.ReadTimeout("t3"),
    ])

    with pytest.raises(httpx.ReadTimeout, match="t3"):
        invoke_with_retry(call, step="t")
    assert calls["n"] == 3
