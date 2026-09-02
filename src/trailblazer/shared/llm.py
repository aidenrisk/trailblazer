"""
LLM client factory.

Reads LLM_PROVIDER (and provider-specific keys) from the environment/.env
and returns a configured LangChain chat model. Keeps every LLM-using agent
provider-agnostic — swap providers via .env, no code changes.

NOTE: nothing calls this today. Frontier is fully deterministic (it explores
every control one by one, so it never has to guess which ones branch), and the
remaining agents aren't built yet. Kept as the seam for the first agent that
does need judgment — Frontier's `value_provider` is one candidate, since
picking a realistic value for a field is a judgment call.
"""

import os

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

load_dotenv()

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def get_model(temperature: float = 0.0) -> BaseChatModel:
    """
    Build the chat model selected by LLM_PROVIDER.

    - "openrouter" (default): ChatOpenAI pointed at OpenRouter's OpenAI-compatible
      endpoint, using OPENROUTER_MODEL (e.g. "x-ai/grok-4.5").
    - "anthropic": ChatAnthropic using ANTHROPIC_MODEL directly against the
      Anthropic Console API.
    """
    provider = os.getenv("LLM_PROVIDER", "openrouter").lower()

    if provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set (required when LLM_PROVIDER=openrouter)")
        model = os.getenv("OPENROUTER_MODEL", "x-ai/grok-4.5")
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            temperature=temperature,
        )

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (required when LLM_PROVIDER=anthropic)")
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        return ChatAnthropic(model=model, api_key=api_key, temperature=temperature)

    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r} (expected 'openrouter' or 'anthropic')")
