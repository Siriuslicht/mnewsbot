from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut

from app.services.telegram import (
    MAX_MESSAGE_LENGTH,
    MessageRejectedError,
    TelegramConfigError,
    TelegramPublisher,
    TelegramUnavailableError,
    format_news_message,
)


def make_bot(**overrides: object) -> AsyncMock:
    bot = AsyncMock()
    bot.id = 999
    bot.username = "test_bot"
    bot.get_chat.return_value = SimpleNamespace(id=-100123, type="channel")
    bot.get_chat_member.return_value = SimpleNamespace(
        status="administrator", can_post_messages=True
    )
    for name, value in overrides.items():
        setattr(bot, name, value)
    return bot


def make_publisher(bot: AsyncMock, **kwargs: object) -> tuple[TelegramPublisher, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    publisher = TelegramPublisher(
        "unused", "@test_channel", bot=bot, sleep=fake_sleep, **kwargs  # type: ignore[arg-type]
    )
    return publisher, sleeps


# ---- formatting ---------------------------------------------------------


def test_format_escapes_html(make_item) -> None:  # type: ignore[no-untyped-def]
    item = make_item(1, text="S&P <500> up", link="https://example.com/a?b=1&c=\"2\"")
    message = format_news_message(item)
    assert "S&amp;P &lt;500&gt; up" in message
    assert '<a href="https://example.com/a?b=1&amp;c=&quot;2&quot;">Source...</a>' in message


def test_footer_is_only_a_source_link(make_item) -> None:  # type: ignore[no-untyped-def]
    message = format_news_message(make_item(1, link="https://x.com/DeItaone/status/2125215045726638437"))
    assert message.endswith(
        '\n\n<a href="https://x.com/DeItaone/status/2125215045726638437">Source...</a>'
    )
    assert "x.com" not in message.replace('href="https://x.com/DeItaone/status/2125215045726638437"', "")


def test_format_uses_uzbek_translation(make_item) -> None:  # type: ignore[no-untyped-def]
    item = make_item(1, text="BITCOIN FALLS").model_copy(
        update={"title_uz": "Bitcoin <tushdi>", "description_uz": "Narx 60 000 dollar"}
    )
    message = format_news_message(item)
    assert "<b>Bitcoin &lt;tushdi&gt;</b>\nNarx 60 000 dollar" in message
    assert "BITCOIN FALLS" not in message


def test_format_has_no_category_or_direction(make_item) -> None:  # type: ignore[no-untyped-def]
    message = format_news_message(make_item(1, cat="STOCKS", dir="bull"))
    assert "Category" not in message
    assert "Direction" not in message


def test_format_drops_unsafe_links(make_item) -> None:  # type: ignore[no-untyped-def]
    message = format_news_message(make_item(1, link="javascript:alert(1)"))
    assert "javascript" not in message
    assert "Source" not in message


def test_format_truncates_without_breaking_entities(make_item) -> None:  # type: ignore[no-untyped-def]
    item = make_item(1, text="&" * 5000)  # each & becomes &amp; (5x longer)
    message = format_news_message(item)
    assert len(message) <= MAX_MESSAGE_LENGTH
    body = message.split("\n")[2]
    assert body.endswith("…")
    assert body[:-1].replace("&amp;", "") == ""  # only whole entities


# ---- sending & retries --------------------------------------------------


async def test_send_success() -> None:
    bot = make_bot()
    publisher, sleeps = make_publisher(bot)
    await publisher.send_text("hi")
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.kwargs["parse_mode"] == "HTML"
    assert sleeps == []


async def test_network_errors_retry_with_backoff() -> None:
    bot = make_bot()
    bot.send_message.side_effect = [NetworkError("reset"), TimedOut(), None]
    publisher, sleeps = make_publisher(bot)
    await publisher.send_text("hi")
    assert bot.send_message.await_count == 3
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]  # exponential: ~1s then ~2s


async def test_network_errors_give_up() -> None:
    bot = make_bot()
    bot.send_message.side_effect = NetworkError("down")
    publisher, sleeps = make_publisher(bot, max_attempts=3)
    with pytest.raises(TelegramUnavailableError):
        await publisher.send_text("hi")
    assert bot.send_message.await_count == 3


async def test_retry_after_is_honoured() -> None:
    bot = make_bot()
    bot.send_message.side_effect = [RetryAfter(7), None]
    publisher, sleeps = make_publisher(bot)
    await publisher.send_text("hi")
    assert sleeps == [7.0]


async def test_long_retry_after_gives_up_for_this_cycle() -> None:
    bot = make_bot()
    bot.send_message.side_effect = RetryAfter(600)
    publisher, sleeps = make_publisher(bot)
    with pytest.raises(TelegramUnavailableError, match="rate limited"):
        await publisher.send_text("hi")
    assert sleeps == []


async def test_bad_message_is_rejected_without_retry() -> None:
    bot = make_bot()
    bot.send_message.side_effect = BadRequest("Can't parse entities")
    publisher, _ = make_publisher(bot)
    with pytest.raises(MessageRejectedError):
        await publisher.send_text("<b>")
    assert bot.send_message.await_count == 1


@pytest.mark.parametrize(
    "error", [BadRequest("Chat not found"), Forbidden("bot was kicked from the channel")]
)
async def test_config_errors_are_unavailable_not_rejected(error: Exception) -> None:
    bot = make_bot()
    bot.send_message.side_effect = error
    publisher, _ = make_publisher(bot)
    with pytest.raises(TelegramUnavailableError):
        await publisher.send_text("hi")
    assert bot.send_message.await_count == 1


# ---- startup validation -------------------------------------------------


async def test_start_validates_permissions() -> None:
    bot = make_bot()
    publisher, _ = make_publisher(bot)
    await publisher.start()
    bot.get_chat_member.assert_awaited_once_with(-100123, 999)


async def test_start_fails_without_post_permission() -> None:
    bot = make_bot()
    bot.get_chat_member.return_value = SimpleNamespace(status="administrator", can_post_messages=False)
    publisher, _ = make_publisher(bot)
    with pytest.raises(TelegramConfigError, match="Post messages"):
        await publisher.start()


async def test_start_fails_when_not_admin() -> None:
    bot = make_bot()
    bot.get_chat_member.return_value = SimpleNamespace(status="member")
    publisher, _ = make_publisher(bot)
    with pytest.raises(TelegramConfigError):
        await publisher.start()


async def test_start_fails_on_unknown_channel() -> None:
    bot = make_bot()
    bot.get_chat.side_effect = BadRequest("Chat not found")
    publisher, _ = make_publisher(bot)
    with pytest.raises(TelegramConfigError, match="Cannot access"):
        await publisher.start()


async def test_start_fails_on_invalid_token_without_leaking_it() -> None:
    secret = "123456789:SECRET_abcdefghijklmnopqrstuvwxyz"
    bot = make_bot()
    bot.initialize.side_effect = InvalidToken(f"The token `{secret}` was rejected")
    publisher, _ = make_publisher(bot)
    with pytest.raises(TelegramConfigError) as info:
        await publisher.start()
    assert secret not in str(info.value)


async def test_check_connection_is_cached() -> None:
    bot = make_bot()
    publisher, _ = make_publisher(bot)
    assert await publisher.check_connection()
    assert await publisher.check_connection()
    assert bot.get_me.await_count == 1
