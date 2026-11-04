"""Application configuration loaded from environment variables / .env file."""

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Telegram bot tokens look like "123456789:AAH...". Used for validation and log redaction.
BOT_TOKEN_PATTERN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")

_CHANNEL_USERNAME = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{3,31}$")
_CHANNEL_NUMERIC = re.compile(r"^-?\d+$")
_TME_LINK = re.compile(r"^@?(?:https?://)?(?:www\.)?t(?:elegram)?\.me/([A-Za-z0-9_]+)/?$")


class Settings(BaseSettings):
    """All runtime settings in one place.

    Values are read from real environment variables first, then from `.env`.
    In production you can skip `.env` entirely and pass real env vars.
    Secrets must never be hardcoded here — only declared.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "production"] = Field(
        "development", alias="ENVIRONMENT"
    )

    # Telegram. SecretStr keeps the token out of repr() and accidental logging.
    bot_token: SecretStr = Field(..., alias="BOT_TOKEN")
    channel_id: str = Field(..., alias="CHANNEL_ID")

    # Polling
    poll_interval: int = Field(30, alias="POLL_INTERVAL", ge=5, le=3600)
    # On the very first run (empty database) remember the current batch
    # without posting it, so starting the bot doesn't flood the channel.
    initial_sync_mode: bool = Field(True, alias="INITIAL_SYNC_MODE")

    # Storage
    database_url: str = Field("sqlite:///./database.db", alias="DATABASE_URL")
    # Lock file that guarantees only one scheduler runs (see app/utils/lock.py).
    lock_file: str = Field("./scheduler.lock", alias="LOCK_FILE")

    # MarketNewsFeed API
    marketnews_url: str = Field(
        "https://marketnewsfeed.com/api/feed?limit=50", alias="MARKETNEWS_URL"
    )
    http_timeout: float = Field(10.0, alias="HTTP_TIMEOUT", gt=0)

    # Pause between consecutive Telegram posts. Telegram limits bots to roughly
    # 20 messages per minute in a single channel.
    post_delay: float = Field(3.0, alias="POST_DELAY", ge=0)
    # Attempts per message for transient errors (network, timeouts, rate limits).
    telegram_max_attempts: int = Field(4, alias="TELEGRAM_MAX_ATTEMPTS", ge=1, le=10)

    # How long shutdown waits for an in-flight cycle before cancelling it.
    shutdown_timeout: float = Field(15.0, alias="SHUTDOWN_TIMEOUT", ge=0)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field("INFO", alias="LOG_LEVEL")
    # "json" for log aggregation in production, "text" for humans.
    log_format: Literal["text", "json"] = Field("text", alias="LOG_FORMAT")
    # Optional rotating log file for VPS runs without Docker. Empty = stdout only.
    log_file: str = Field("", alias="LOG_FILE")

    @field_validator("bot_token")
    @classmethod
    def _validate_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if not BOT_TOKEN_PATTERN.fullmatch(token):
            raise ValueError("BOT_TOKEN must look like '123456789:AA...' (get it from @BotFather)")
        return SecretStr(token)

    @field_validator("channel_id")
    @classmethod
    def _validate_channel(cls, value: str) -> str:
        """Accept '@name' or a numeric id; normalise t.me links to '@name'."""
        value = value.strip()
        if link := _TME_LINK.match(value):
            value = f"@{link.group(1)}"
        if _CHANNEL_USERNAME.match(value) or _CHANNEL_NUMERIC.match(value):
            return value
        raise ValueError(
            "CHANNEL_ID must be '@channel_username' or a numeric id like -1001234567890"
        )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so .env is parsed only once."""
    return Settings()  # type: ignore[call-arg]  # values come from env
