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
    if _is_malformed_structured_output(error):
        return True
    return isinstance(error, ValueError) and bool(_TRANSIENT_STATUS.search(str(error)))


def _is_malformed_structured_output(error: BaseException) -> bool:
    """A structured reply that arrived truncated or otherwise unparseable.

    The request went through, the provider answered, and the answer was cut off
    mid-JSON -- the same class of failure as `TransientModelError`'s empty
    reply, and asking again usually gets a whole one. A live run died on
    "Unterminated string starting at: line 1 column 350" while describing a
    23-control page, having spent nine minutes and four pages of walking.

    Matched by class name rather than by import: the exception lives in
    `langchain.agents.structured_output`, which is a private-ish path that has
    moved between versions, and a failed import here would silently stop every
    retry this function grants.
    """
    return any(
        c.__name__ in ("StructuredOutputValidationError", "StructuredOutputError")
        for c in type(error).__mro__
    )


_REQUEST_TIMEOUT_MS = 90_000
"""Per-request budget for one model call, on the transport. `ChatOpenRouter`
takes milliseconds (its `timeout_ms`), not seconds."""

_MAX_OUTPUT_TOKENS = 16_384
"""Ceiling on one reply's length.

Declared rather than left to the client, which sends the model's own maximum --
65,536 for grok-4.5. Tokens are billed on what is generated, but OpenRouter
checks affordability against input plus this ceiling *before* forwarding, so an
oversized reservation refuses a request the account could pay for: a run died
on "you requested up to 65536 tokens, but can only afford 65283".

Sized from measurement. Across 352 logged calls the longest reply was 3,230
output tokens, a perceive of a 23-control page; a vision reading peaked at 1,439
and the value chooser at 403. Five times the worst observed case, so a page
description is not truncated mid-structure."""


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


def get_vision_model(settings: Settings | None = None) -> BaseChatModel:
    """The model the vision fallback sends its screenshot to.

    Separate from `get_model` because the crawl's model is chosen for structured
    output over text and need not accept an image; `VISION_MODEL` names one that
    does. Same provider and key, so nothing else is configured twice.
    """
    settings = settings or get_settings()
    if settings.llm_provider != "openrouter":
        return get_model(settings)
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set; put it in .env")
    from langchain_openrouter import ChatOpenRouter

    return ChatOpenRouter(
        model=settings.vision_model,
        temperature=0,
        openrouter_api_key=settings.openrouter_api_key,
        openrouter_provider={"require_parameters": True},
        request_timeout=_REQUEST_TIMEOUT_MS,
        max_retries=0,
    )


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
            # A look answers in 10-25 s. Without this the client waits its
            # library default of ten minutes on a dropped connection before
            # `invoke_with_retry` ever sees an error; one run froze that way.
            request_timeout=_REQUEST_TIMEOUT_MS,
            max_tokens=_MAX_OUTPUT_TOKENS,
            max_retries=0,  # retries belong to `invoke_with_retry`, which logs them
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
        max_tokens=_MAX_OUTPUT_TOKENS,
    )
