"""Runtime settings, read from `.env` or the environment."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything the scraper needs that is not code. See `.env.example`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: Literal["openrouter", "anthropic"] = "openrouter"

    openrouter_api_key: str | None = None
    openrouter_model: str = "x-ai/grok-4.5"

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-5"

    scraper_perceiver: Literal["dom_snapshot", "a11y"] = "dom_snapshot"
    """Which `Perceiver` implementation builds the payload handed to the model."""

    headed: bool = False
    cdp_port: int = 9222
    """Chrome itself commonly holds 9222; override when the launch reports it busy."""

    log_level: str = "INFO"
    """Level for the `trailblazer` logger. DEBUG adds payload sizes and locator misses."""

    carrier_url: str | None = None
    """Dev-only stand-in for `carrier_creds.login_url`. See `dev_carrier_creds.py`."""

    carrier_username: str | None = None
    """Dev-only stand-in for `carrier_creds.username`. See `dev_carrier_creds.py`."""

    carrier_password: str | None = None
    """Dev-only stand-in for `carrier_creds.password`. See `dev_carrier_creds.py`."""


def get_settings() -> Settings:
    """Load settings fresh. Cheap, and keeps tests free to patch the environment."""
    return Settings()
