"""Provider switch. OpenRouter is primary; an Anthropic Console key is the second path.

No Claude Code OAuth token: the Messages API rejects `sk-ant-oat01-*` with
"OAuth authentication is currently not supported", and the terms restrict it to
Anthropic's own clients.
"""

import re
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from langchain_core.language_models import BaseChatModel

from trailblazer.observability.logging import get_logger
from trailblazer.shared.config import Settings, get_settings

log = get_logger(__name__)

T = TypeVar("T")

_ATTEMPTS = 3
_BACKOFF_S = (1.0, 3.0)
"""Waits before the second and third attempts. Short: a provider that is still
failing after four seconds is not going to recover inside this call, and the
crawl has a browser session open the whole time."""

_TRANSIENT_STATUS = re.compile(r"\(code: (429|5\d\d)\)")
"""The OpenRouter client reports a provider-side failure as a `ValueError` whose
message ends in `(code: N)`. Rate limits and server errors are worth a retry; a
400-class code is a malformed request and is not."""


class TransientModelError(RuntimeError):
    """A model call that returned nothing usable and is worth asking again.

    Raised by a caller that got a response with no content: the request went
    through, the provider answered, and the answer was empty. On a live run one
    such reply killed a crawl five restarts deep.
    """


def _is_transient(error: BaseException) -> bool:
    if isinstance(error, (httpx.HTTPError, TransientModelError)):
        return True
    return isinstance(error, ValueError) and bool(_TRANSIENT_STATUS.search(str(error)))


def invoke_with_retry(call: Callable[[], T], *, step: str) -> T:
    """Run `call`, retrying a transient failure up to `_ATTEMPTS` times.

    A crawl makes dozens of model calls over ten minutes or more; at that length
    one dropped connection or one empty reply is close to certain, and without
    this a single one ended the run. Two live runs died that way.

    Retried: an httpx transport error (a dropped connection, a timeout), the
    OpenRouter client's `ValueError` for a 429 or 5xx, and a caller's
    `TransientModelError`. Anything else is raised at once -- a 400-class error,
    a schema failure, a bug -- because asking again cannot change the answer.
    The last error is re-raised after the final attempt, so nothing is
    swallowed and the failure still names its cause.
    """
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            return call()
        except Exception as error:  # noqa: BLE001 -- classified below, never swallowed
            if not _is_transient(error) or attempt == _ATTEMPTS:
                raise
            wait = _BACKOFF_S[attempt - 1]
            log.warning(
                "model call failed step=%s attempt=%d/%d retry_in=%.0fs error=%s: %s",
                step,
                attempt,
                _ATTEMPTS,
                wait,
                type(error).__name__,
                str(error)[:160],
            )
            time.sleep(wait)
    raise AssertionError("unreachable: the loop returns or raises")


def get_model(settings: Settings | None = None) -> BaseChatModel:
    """Build the chat model named by `LLM_PROVIDER`, at temperature 0.

    For OpenRouter, `require_parameters` restricts routing to endpoints that
    actually implement structured output -- support varies by endpoint, not just
    by model, so without it a request can land somewhere that ignores the schema.
    """
    settings = settings or get_settings()

    if settings.llm_provider == "openrouter":
        if not settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set; put it in .env")
        from langchain_openrouter import ChatOpenRouter

        return ChatOpenRouter(
            model=settings.openrouter_model,
            temperature=0,
            openrouter_api_key=settings.openrouter_api_key,
            openrouter_provider={"require_parameters": True},
        )

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set; an Anthropic Console key is required for "
            "LLM_PROVIDER=anthropic (a Claude Code OAuth token will not work)"
        )
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=settings.anthropic_model,
        temperature=0,
        api_key=settings.anthropic_api_key,
    )
