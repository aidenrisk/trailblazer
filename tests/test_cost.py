"""Cost parsing, fed fake `LLMResult`s. No provider, no API key.

The two providers hand cost back in different shapes, and the Anthropic one has
a trap in it, so both are pinned here against constructed responses rather than
against a live call that would cost money to run.
"""

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from trailblazer.observability.cost import CostTracker


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


def test_anthropic_cost_prices_cache_buckets_separately() -> None:
    """The trap: `input_tokens` already contains the cache buckets.

    langchain_anthropic adds cache_read and cache_creation back into
    `input_tokens`, so charging that total at the base rate overcharges. Here
    1000 plain + 4000 read + 1000 write must be priced 1000 at 1.0x, 4000 at
    0.1x and 1000 at 1.25x -- not 6000 at 1.0x.
    """
    tracker = CostTracker(step="perceive")
    tracker.on_llm_end(
        _result(
            AIMessage(
                content="ok",
                response_metadata={"model_name": "claude-sonnet-4-5"},
                usage_metadata={
                    "input_tokens": 6000,  # 1000 plain + 4000 read + 1000 write
                    "output_tokens": 500,
                    "total_tokens": 6500,
                    "input_token_details": {"cache_read": 4000, "cache_creation": 1000},
                },
            )
        )
    )

    input_rate, output_rate = 3.0 / 1e6, 15.0 / 1e6
    expected = (
        1000 * input_rate + 4000 * input_rate * 0.1 + 1000 * input_rate * 1.25 + 500 * output_rate
    )
    assert tracker.calls[0]["usd"] == expected

    naive = 6000 * input_rate + 500 * output_rate
    assert tracker.calls[0]["usd"] < naive  # the overcharge is avoided


def test_unpriced_anthropic_model_reports_unknown_not_a_guess(caplog) -> None:
    """A model missing from the table is reported as unknown, never estimated."""
    tracker = CostTracker(step="perceive")
    with caplog.at_level("WARNING", logger="trailblazer"):
        tracker.on_llm_end(
            _result(
                AIMessage(
                    content="ok",
                    response_metadata={"model_name": "claude-something-unreleased"},
                    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )
            )
        )

    assert tracker.calls[0]["usd"] is None
    assert tracker.total_usd() is None
    assert "no price for model" in caplog.text


def test_every_call_in_the_loop_is_recorded_and_totalled() -> None:
    """The tool loop plus the structured-output call are separate rows."""
    tracker = CostTracker(step="perceive")
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


def test_missing_usage_metadata_does_not_raise() -> None:
    """Some providers omit usage entirely; the row is still logged."""
    tracker = CostTracker(step="perceive")
    tracker.on_llm_end(
        _result(AIMessage(content="ok", response_metadata={"model_name": "x-ai/grok-4.5"}))
    )

    row = tracker.calls[0]
    assert (row["input_tokens"], row["output_tokens"], row["usd"]) == (None, None, None)
