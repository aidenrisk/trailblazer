"""Per-LLM-call USD cost, logged and nowhere else.

Cost is deliberately kept out of `ScraperResult` and off disk: it is an
operational number, not part of any contract the pipeline consumes.

Two providers, two mechanisms:

- **OpenRouter** returns the amount it actually charged in
  `message.response_metadata["cost"]` (langchain_openrouter surfaces it at
  chat_models.py:859-867). Nothing has to be requested -- `usage: {include:
  true}` is deprecated and a no-op. OpenRouter documents that number as
  *credits*; for a standard account credits are 1:1 with USD, which is the
  assumption made here.
- **Anthropic** returns no cost at all, so it is priced locally from
  `_ANTHROPIC_PRICES`. The trap: langchain_anthropic adds cache_read and
  cache_creation tokens *back into* `usage_metadata["input_tokens"]`
  (chat_models.py:2963-2972), so multiplying that total by the base input rate
  overcharges. The buckets are priced separately from `input_token_details`,
  which bill at roughly 0.1x (read) and 1.25x (write).

A model missing from the table reports `usd=None` and logs a warning, rather
than being priced with a guessed rate.
"""

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from trailblazer.observability.logging import get_logger

log = get_logger(__name__)

# USD per token, Anthropic Console list prices. Only the models this project
# can be configured to use; anything else is reported as unknown.
_ANTHROPIC_PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.0 / 1e6, 15.0 / 1e6),
    "claude-haiku-4-5": (1.0 / 1e6, 5.0 / 1e6),
    "claude-opus-4-5": (5.0 / 1e6, 25.0 / 1e6),
}

_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25


def _anthropic_usd(model: str, usage: dict[str, Any]) -> float | None:
    """Price an Anthropic call from token buckets, or None if the model is unpriced."""
    rates = next((v for k, v in _ANTHROPIC_PRICES.items() if model.startswith(k)), None)
    if rates is None:
        log.warning("no price for model=%s; reporting usd=unknown", model)
        return None
    input_rate, output_rate = rates

    details = usage.get("input_token_details") or {}
    cache_read = details.get("cache_read") or 0
    cache_write = details.get("cache_creation") or 0
    # input_tokens already includes both cache buckets, so subtract them out
    # before charging the remainder at the full input rate.
    plain = max((usage.get("input_tokens") or 0) - cache_read - cache_write, 0)

    return (
        plain * input_rate
        + cache_read * input_rate * _CACHE_READ_MULTIPLIER
        + cache_write * input_rate * _CACHE_WRITE_MULTIPLIER
        + (usage.get("output_tokens") or 0) * output_rate
    )


class CostTracker(BaseCallbackHandler):
    """One row per LLM call, logged as it happens and kept for a step total.

    Passed as `config={"callbacks": [tracker]}` to `agent.invoke()`, so it sees
    every call the agent loop makes -- including the extra structured-output
    call at the end.
    """

    def __init__(self, step: str, job_id: str | None = None) -> None:
        self.step = step
        self.job_id = job_id
        self.calls: list[dict[str, Any]] = []

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Read cost and usage off the returned message and log one line."""
        message = response.generations[0][0].message
        usage = message.usage_metadata or {}
        model = message.response_metadata.get("model_name") or ""

        usd = message.response_metadata.get("cost")
        if usd is None:
            usd = _anthropic_usd(model, usage)

        row = {
            "model": model,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "usd": usd,
        }
        self.calls.append(row)

        log.info(
            "llm call step=%s job_id=%s model=%s in_tokens=%s out_tokens=%s usd=%s",
            self.step,
            self.job_id,
            model,
            row["input_tokens"],
            row["output_tokens"],
            "unknown" if usd is None else f"{usd:.6f}",
        )

    def total_usd(self) -> float | None:
        """Sum of the priced calls, or None when any call could not be priced."""
        if any(c["usd"] is None for c in self.calls):
            return None
        return sum(c["usd"] for c in self.calls)
