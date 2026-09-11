"""Per-LLM-call USD cost, logged and nowhere else.

Cost is deliberately kept out of `ScraperResult` and off disk: it is an
operational number, not part of any contract the pipeline consumes.

OpenRouter returns the amount it actually charged in
`message.response_metadata["cost"]` (langchain_openrouter surfaces it at
chat_models.py:859-867). Nothing has to be requested -- `usage: {include:
true}` is deprecated and a no-op. OpenRouter documents that number as
*credits*; for a standard account credits are 1:1 with USD, which is the
assumption made here.

A response with no cost reports `usd=None` and logs a warning, rather than
being priced with a guessed rate.
"""

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from trailblazer.observability.ledger import RunLedger
from trailblazer.observability.logging import get_logger

log = get_logger(__name__)

# CostTracker.step is the call site; the ledger groups spend by agent.
_LEDGER_AGENT = {
    "perceive": "scraper",
    "choose_value": "form_filler",
    "vision": "vision",
}


class CostTracker(BaseCallbackHandler):
    """One row per LLM call, logged as it happens and kept for a step total.

    Passed as `config={"callbacks": [tracker]}` to `agent.invoke()`, so it sees
    every call the agent loop makes -- including the extra structured-output
    call at the end.

    Each completed call is posted to `ledger` immediately. A later exception
    cannot drop spend that has already been billed.
    """

    def __init__(
        self, step: str, job_id: str | None = None, ledger: RunLedger | None = None
    ) -> None:
        self.step = step
        self.ledger = ledger
        self.job_id = job_id or (ledger.job_id if ledger is not None else None)
        self.calls: list[dict[str, Any]] = []

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Read cost and usage off the returned message and log one line."""
        message = response.generations[0][0].message
        usage = message.usage_metadata or {}
        model = message.response_metadata.get("model_name") or ""

        usd = message.response_metadata.get("cost")
        if usd is None:
            log.warning("no cost in response for model=%s; reporting usd=unknown", model)

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
        if self.ledger is not None:
            self.ledger.record(
                agent=_LEDGER_AGENT.get(self.step, self.step),
                action=self.step,
                detail=model,
                usd=0.0 if usd is None else float(usd),
                unpriced=usd is None,
            )

    def total_usd(self) -> float | None:
        """Sum of the priced calls, or None when any call could not be priced."""
        if any(c["usd"] is None for c in self.calls):
            return None
        return sum(c["usd"] for c in self.calls)
