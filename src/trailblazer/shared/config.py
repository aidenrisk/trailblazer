"""Runtime settings, read from `.env` or the environment."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything the pipeline needs that is not code. See `.env.example`."""

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

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str = "postgresql://postgres:postgres@127.0.0.1:15434/trailblazer"
    """The project's Postgres (docker-compose maps it to 15434). Holds carriers,
    credentials, and login programs."""

    login_lock_database_url: str | None = None
    """Where the per-carrier login lock is taken. Defaults to `database_url`.
    Point it at Roadrunner's Postgres when both engines drive the same carriers,
    so their logins serialise against each other and never cross OTP codes."""

    # ── Credentials at rest ───────────────────────────────────────────────
    cred_encryption_key: str | None = None
    """32-byte AES-256-GCM key as 64 hex or 44 base64 chars. Same format as
    Roadrunner's `RR_CRED_ENCRYPTION_KEY`, so rows and keys are portable between
    the two. Unset means passwords are stored in plaintext, with a warning."""

    # ── MFA inbox (shared backend) ────────────────────────────────────────
    aiden_backend_url: str | None = None
    aiden_app_secret: str | None = None
    aiden_internal_secret: str | None = None
    """The three values `GET {aiden_backend_url}/api/internal/mfa/{slug}/otp`
    needs: the URL, the `x-api-secret` header, the `x-cron-secret` header. All
    three must be set for a code to be pulled; otherwise MFA waits for a human."""

    mfa_timeout_ms: int = 600_000
    """How long a capture waits for a one-time code to clear."""

    login_test_mfa_timeout_ms: int = 180_000
    """The shorter wait a login health check allows; an operator is watching."""

    sessions_dir: str = ".sessions"
    """Where per-carrier cookie jars and browser profiles are kept."""

    @property
    def effective_login_lock_database_url(self) -> str:
        return self.login_lock_database_url or self.database_url


def get_settings() -> Settings:
    """Load settings fresh. Cheap, and keeps tests free to patch the environment."""
    return Settings()
