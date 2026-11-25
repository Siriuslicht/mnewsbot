"""Application configuration loaded from environment variables / .env file."""

import re
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
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

    # X (Twitter) accounts whose items are never posted, comma-separated and
    # case-insensitive. Matched against links like https://x.com/<account>/...
    excluded_x_accounts: str = Field("FirstSquawk,MarketNews_Feed", alias="EXCLUDED_X_ACCOUNTS")
    # X accounts whose items are always posted, instantly: no importance
    # scoring, no daily cap or pacing, no pause between posts.
    priority_x_accounts: str = Field("DeItaone,financialjuice", alias="PRIORITY_X_ACCOUNTS")

    # Importance filter: Claude scores each new headline 1-5 and only items
    # scoring >= MIN_IMPORTANCE are posted, at most DAILY_POST_CAP per day.
    importance_filter: bool = Field(True, alias="IMPORTANCE_FILTER")
    # Translate posts into Uzbek (Latin script) with Claude.
    translate_uz: bool = Field(True, alias="TRANSLATE_UZ")
    anthropic_api_key: SecretStr | None = Field(None, alias="ANTHROPIC_API_KEY")
    claude_model: str = Field("claude-haiku-5-5", alias="CLAUDE_MODEL")
    claude_timeout: float = Field(30.0, alias="CLAUDE_TIMEOUT", gt=0)
    min_importance: int = Field(4, alias="MIN_IMPORTANCE", ge=1, le=5)
    # Cost saving: headlines are scored together, up to SCORE_BATCH_SIZE per
    # Claude call, waiting at most SCORE_MAX_WAIT seconds after publication.
    score_batch_size: int = Field(20, alias="SCORE_BATCH_SIZE", ge=1, le=50)
    score_max_wait: int = Field(300, alias="SCORE_MAX_WAIT", ge=0, le=1800)
    daily_post_cap: int = Field(200, alias="DAILY_POST_CAP", ge=1)
    # Posts are spread over the day: by 12:00 at most ~half the cap is used.
    # POST_BURST lets this many extra posts through when a lot happens at once.
    post_burst: int = Field(10, alias="POST_BURST", ge=0)
    # The daily cap resets at midnight in this timezone.
    bot_timezone: str = Field("Asia/Tashkent", alias="BOT_TIMEZONE")

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

    @property
    def excluded_accounts(self) -> frozenset[str]:
        return _account_set(self.excluded_x_accounts)

    @property
    def priority_accounts(self) -> frozenset[str]:
        return _account_set(self.priority_x_accounts) - self.excluded_accounts

    @field_validator("bot_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"BOT_TIMEZONE '{value}' is not a valid timezone name") from None
        return value

    @model_validator(mode="after")
    def _require_claude_key(self) -> "Settings":
        # Fail at startup rather than silently posting every item unfiltered.
        if (self.importance_filter or self.translate_uz) and not self.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required while IMPORTANCE_FILTER=true or TRANSLATE_UZ=true "
                "(set both to false to post every item unfiltered, in English)"
            )
        return self

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


def _account_set(value: str) -> frozenset[str]:
    return frozenset(name.strip().lstrip("@").lower() for name in value.split(",") if name.strip())


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so .env is parsed only once."""
    return Settings()  # type: ignore[call-arg]  # values come from env
