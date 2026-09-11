"""Cost parsing, fed fake `LLMResult`s. No provider, no API key.

OpenRouter hands the charged amount back in response metadata, so the tracker
is pinned here against a constructed response rather than a live call that
would cost money to run.
"""

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from trailblazer.observability.cost import CostTracker
from trailblazer.observability.ledger import RunLedger


def _result(message: AIMessage) -> LLMResult:
    """Wrap one message the way a chat model's callback receives it."""
    return LLMResult(generations=[[ChatGeneration(message=message)]])


def test_openrouter_cost_is_read_from_response_metadata() -> None:
    """OpenRouter returns the amount actually charged; nothing is recomputed."""
    tracker = CostTracker(step="perceive", job_id="j1")
    tracker.on_llm_end(
        _result(
            AIMessage(
                content="ok",
                response_metadata={"model_name": "x-ai/grok-4.5", "cost": 0.004216},
                usage_metadata={"input_tokens": 3410, "output_tokens": 880, "total_tokens": 4290},
            )
        )
    )

    row = tracker.calls[0]
    assert row["usd"] == 0.004216
    assert (row["input_tokens"], row["output_tokens"]) == (3410, 880)
    assert row["model"] == "x-ai/grok-4.5"


def test_missing_cost_reports_unknown_not_a_guess(caplog) -> None:
    """A reply with no OpenRouter cost is reported as unknown, never estimated."""
    ledger = RunLedger(job_id="j1")
    tracker = CostTracker(step="vision", ledger=ledger)
    with caplog.at_level("WARNING", logger="trailblazer"):
        tracker.on_llm_end(
            _result(
                AIMessage(
                    content="ok",
                    response_metadata={"model_name": "x-ai/grok-4.5"},
                    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )
            )
        )

    assert tracker.calls[0]["usd"] is None
    assert tracker.total_usd() is None
    assert "no cost in response" in caplog.text
    assert ledger.steps[0].unpriced is True
    assert ledger.steps[0].usd == 0.0
    assert ledger.steps[0].agent == "vision"


def test_every_call_in_the_loop_is_recorded_and_totalled() -> None:
    """The tool loop plus the structured-output call are separate rows."""
    ledger = RunLedger(job_id="j1")
    tracker = CostTracker(step="perceive", job_id="j1", ledger=ledger)
    for cost in (0.001, 0.002, 0.0005):
        tracker.on_llm_end(
            _result(
                AIMessage(
                    content="ok",
                    response_metadata={"model_name": "x-ai/grok-4.5", "cost": cost},
                    usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
            )
        )

    assert len(tracker.calls) == 3
    assert tracker.total_usd() == 0.0035
    assert [s.usd for s in ledger.steps] == [0.001, 0.002, 0.0005]
    assert ledger.total_usd() == 0.0035
    assert all(s.agent == "scraper" and s.action == "perceive" for s in ledger.steps)


def test_missing_usage_metadata_does_not_raise() -> None:
    """Some providers omit usage entirely; the row is still logged."""
    tracker = CostTracker(step="perceive")
    tracker.on_llm_end(
        _result(AIMessage(content="ok", response_metadata={"model_name": "x-ai/grok-4.5"}))
    )

    row = tracker.calls[0]
    assert (row["input_tokens"], row["output_tokens"], row["usd"]) == (None, None, None)
