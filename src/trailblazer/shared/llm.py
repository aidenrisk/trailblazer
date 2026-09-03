"""
LLM client factory.

Reads LLM_PROVIDER (and provider-specific keys) from the environment/.env and
returns a configured LangChain chat model. Keeps every LLM-using agent
provider-agnostic — swap providers via .env, no code changes.

FormFiller is the first caller: it decides what to type into a field, which is
the one genuine judgment call in a walk (see agents/form_filler/value_picker.py).
Everything else stays deterministic.

Imports are deliberately inside each branch. Only one provider's package needs
to be installed for that provider to work, and a top-level import of all of them
means an unused provider's absence breaks the module for everyone — which is
exactly how this file sat broken while it had no callers.
"""

import os

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel

load_dotenv()

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def get_model(temperature: float = 0.0) -> BaseChatModel:
    """
    Build the chat model selected by LLM_PROVIDER.

    - "openrouter" (default): OPENROUTER_MODEL (e.g. "x-ai/grok-4.5"), via the
      dedicated langchain-openrouter package when it is installed and via
      langchain-openai pointed at OpenRouter's OpenAI-compatible endpoint when
      it is not. Same endpoint either way.
    - "anthropic": ChatAnthropic using ANTHROPIC_MODEL directly against the
      Anthropic Console API.
    """
    provider = os.getenv("LLM_PROVIDER", "openrouter").lower()

    if provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set (required when LLM_PROVIDER=openrouter)"
            )
        model = os.getenv("OPENROUTER_MODEL", "x-ai/grok-4.5")
        try:
            from langchain_openrouter import ChatOpenRouter

            return ChatOpenRouter(model=model, api_key=api_key, temperature=temperature)
        except ImportError:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=model,
                api_key=api_key,
                base_url=OPENROUTER_BASE_URL,
                temperature=temperature,
            )

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set (required when LLM_PROVIDER=anthropic)"
            )
        from langchain_anthropic import ChatAnthropic

        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        return ChatAnthropic(model=model, api_key=api_key, temperature=temperature)

    raise ValueError(
        f"Unknown LLM_PROVIDER: {provider!r} (expected 'openrouter' or 'anthropic')"
    )
