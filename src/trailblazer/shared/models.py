"""Provider switch. OpenRouter is primary; an Anthropic Console key is the second path.

No Claude Code OAuth token: the Messages API rejects `sk-ant-oat01-*` with
"OAuth authentication is currently not supported", and the terms restrict it to
Anthropic's own clients.
"""

from langchain_core.language_models import BaseChatModel

from trailblazer.shared.config import Settings, get_settings


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
