import logging

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.utils.logger import Redactor, TextFormatter

TOKEN = "123456789:TEST_token_abcdefghijklmnopqrstuvwxyz"


def make_settings(**env: str) -> Settings:
    values = {"BOT_TOKEN": TOKEN, "CHANNEL_ID": "@test_channel", **env}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@finbrok_marketnews", "@finbrok_marketnews"),
        ("-1001234567890", "-1001234567890"),
        ("https://t.me/finbrok_marketnews", "@finbrok_marketnews"),
        ("@t.me/finbrok_marketnews", "@finbrok_marketnews"),
        ("t.me/finbrok_marketnews", "@finbrok_marketnews"),
    ],
)
def test_channel_id_is_normalised(raw: str, expected: str) -> None:
    assert make_settings(CHANNEL_ID=raw).channel_id == expected


@pytest.mark.parametrize("raw", ["finbrok", "@a b", "", "@x"])
def test_invalid_channel_id_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(CHANNEL_ID=raw)


def test_invalid_token_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(BOT_TOKEN="not-a-token")


def test_token_is_hidden_in_repr() -> None:
    assert TOKEN not in repr(make_settings())


def test_defaults() -> None:
    settings = make_settings()
    assert settings.poll_interval == 30
    assert settings.initial_sync_mode is True


def test_poll_interval_bounds() -> None:
    with pytest.raises(ValidationError):
        make_settings(POLL_INTERVAL="1")


def test_logs_redact_bot_token() -> None:
    formatter = TextFormatter(Redactor([TOKEN]))
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "The token `%s` was rejected", (TOKEN,), None
    )
    line = formatter.format(record)
    assert TOKEN not in line
    assert "***" in line


def test_filter_requires_claude_key() -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        make_settings(IMPORTANCE_FILTER="true")
    settings = make_settings(IMPORTANCE_FILTER="true", ANTHROPIC_API_KEY="test-key")
    assert settings.daily_post_cap == 200 and settings.min_importance == 3


def test_excluded_accounts_are_normalised() -> None:
    settings = make_settings(EXCLUDED_X_ACCOUNTS=" @FirstSquawk , MarketNews_Feed,")
    assert settings.excluded_accounts == {"firstsquawk", "marketnews_feed"}


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(BOT_TIMEZONE="Mars/Olympus")


def test_claude_key_is_redacted_from_logs() -> None:
    record = logging.LogRecord("x", logging.ERROR, "", 0, "key=sk-ant-secret-123", None, None)
    assert "sk-ant-secret-123" not in TextFormatter(Redactor(["sk-ant-secret-123"])).format(record)
