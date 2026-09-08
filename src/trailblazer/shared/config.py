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

    vision_model: str = "google/gemini-2.5-pro"
    """The model the vision fallback sends its screenshot to. Separate from
    `openrouter_model` because the crawl's model is chosen for structured output
    on text and need not accept an image at all."""

    scraper_perceiver: Literal["dom_snapshot", "a11y"] = "dom_snapshot"
    """Which `Perceiver` implementation builds the payload handed to the model."""

    headed: bool = False
    cdp_port: int = 9333
    """The shared browser every agent attaches to. Not 9222: a desktop Chrome holds that."""

    browser_profile_dir: str = "~/.trailblazer/chrome-profile"
    """Persistent Chromium profile. Holds the login between runs."""

    session_file: str = "~/.trailblazer/session.json"
    """Where `launch` records the live endpoint, so agents need not guess the port."""

    attach_if_running: bool = True
    """Attach to a browser already serving CDP on `cdp_port` instead of launching."""

    artifacts_dir: str = "outputs"
    """Where a crawl writes its questions, metadata and replay script."""

    log_level: str = "INFO"
    """Level for the `trailblazer` logger. DEBUG adds payload sizes and locator misses."""

    crawl_state: str = "California"
    """The state the crawl is scoped to. Reaches the value chooser, which must put
    a real address of that state into every location field: a ZIP from elsewhere
    walks a path the flow does not cover."""

    crawl_business_type: str = ""
    """The business the values must hang together for, set per run by the crawl."""

    carrier_url: str | None = None
    """Dev-only stand-in for `carrier_creds.login_url`. See `dev_carrier_creds.py`."""

    carrier_username: str | None = None
    """Dev-only stand-in for `carrier_creds.username`. See `dev_carrier_creds.py`."""

    carrier_password: str | None = None
    """Dev-only stand-in for `carrier_creds.password`. See `dev_carrier_creds.py`."""


def get_settings() -> Settings:
    """Load settings fresh. Cheap, and keeps tests free to patch the environment."""
    return Settings()
