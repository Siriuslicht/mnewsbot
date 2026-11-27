"""Telegram publishing client.

Reusable: it validates access to a channel, formats news items, and sends
messages with retries. It does not know where news comes from or how
duplicates are tracked.

Errors are translated into two outcomes the caller can act on:
- MessageRejectedError: Telegram will never accept THIS message -> skip it.
- TelegramUnavailableError: Telegram/config problem -> stop and retry later.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from html import escape
from typing import TypeVar
from urllib.parse import urlparse

from telegram import Bot
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from app.services.marketnews import AssetTag, NewsItem
from app.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# Telegram rejects messages longer than 4096 characters.
MAX_MESSAGE_LENGTH = 4096
# BadRequest texts that mean "the channel is misconfigured", not "this message is bad".
_CONFIG_ERROR_HINTS = (
    "chat not found",
    "not enough rights",
    "need administrator rights",
    "have no rights",
    "chat_write_forbidden",
    "bot is not a member",
)


class TelegramPublishError(Exception):
    """Base class for delivery failures."""


class MessageRejectedError(TelegramPublishError):
    """Telegram permanently refused this specific message (e.g. bad formatting)."""


class TelegramUnavailableError(TelegramPublishError):
    """Telegram is unreachable, rate limiting, or the bot lost access to the channel."""


class TelegramConfigError(Exception):
    """Startup check failed: bad token, wrong channel, or missing permissions."""


def format_news_message(item: NewsItem) -> str:
    """Build the HTML message for one news item.

    Every API value is HTML-escaped, so `<`, `>` or `&` in the news can never
    break the markup. Long text is shortened BEFORE escaping, so we can never
    cut an entity like `&amp;` in half (which Telegram would reject).
    """
    text_limit = MAX_MESSAGE_LENGTH
    while True:
        message = _build_message(item, text_limit)
        if len(message) <= MAX_MESSAGE_LENGTH:
            return message
        text_limit -= max(len(message) - MAX_MESSAGE_LENGTH, 50)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def _build_message(item: NewsItem, text_limit: int) -> str:
    lines = ["📰 <b>Finbrok Market Pulse</b>", ""]
    if item.tags:
        # Hashtag lines like "⬆️ 🇺🇸 #NVDA = +4,6%", one per asset.
        lines += [_tag_line(tag) for tag in item.tags]
        lines.append("")
    # Flag of the country the news is about, in front of the headline.
    country = item.country or _shared_country(item.tags)
    flag = f"{_flag(country)} " if _flag(country) else ""
    if item.title_uz:
        # Uzbek translation: bold headline, then the short description.
        lines.append(f"{flag}<b>{escape(_shorten(item.title_uz, text_limit), quote=False)}</b>")
        if item.description_uz:
            lines.append(escape(_shorten(item.description_uz, text_limit), quote=False))
    else:
        lines.append(escape(_shorten(item.text, text_limit), quote=False))

    # Footer is just a blue "Source..." link that opens the original article.
    if item.link and _is_safe_url(item.link):
        href = escape(item.link, quote=True)
        lines += ["", f'<a href="{href}">Source...</a>']

    return "\n".join(lines)


def _tag_line(tag: AssetTag) -> str:
    parts: list[str] = []
    # A stated percentage decides the arrow; otherwise Claude's reading of the news.
    rising = tag.change_pct > 0 if tag.change_pct else {"up": True, "down": False}.get(tag.direction)
    if rising is not None:
        parts.append("⬆️" if rising else "⬇️")
    if flag := _flag(tag.country):
        parts.append(flag)
    line = " ".join(parts + [f"#{escape(tag.symbol, quote=False)}"])
    if tag.change_pct is not None:
        line += f" = {_format_pct(tag.change_pct)}"
    return line


def _shared_country(tags: list[AssetTag]) -> str:
    """The tags' country when they all share one (e.g. a list of US stocks)."""
    countries = {tag.country for tag in tags}
    return countries.pop() if len(countries) == 1 else ""


def _flag(country: str) -> str:
    """'US' -> 🇺🇸, 'EU' -> 🇪🇺: two regional-indicator letters render as a flag."""
    if len(country) != 2 or not country.isascii() or not country.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in country.upper())


def _format_pct(value: float) -> str:
    """+4,6% / -2% / 0% with the Uzbek decimal comma."""
    text = f"{value:+.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return ("0" if text in ("+0", "-0") else text) + "%"


def _is_safe_url(url: str) -> bool:
    """Only plain http(s) links; never javascript:, tg:, or other schemes."""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _seconds(value: int | float | timedelta) -> float:
    """python-telegram-bot reports retry_after as seconds or a timedelta."""
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


class TelegramPublisher:
    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        *,
        max_attempts: int = 4,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        max_retry_after: float = 60.0,
        bot: Bot | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # `bot` and `sleep` are injectable so tests can run without network or waiting.
        self._bot = bot or Bot(token=bot_token)
        self._channel_id = channel_id
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._max_retry_after = max_retry_after
        self._sleep = sleep
        self._connection_ok: tuple[float, bool] | None = None  # (checked_at, result)

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Validate the token AND channel permissions once, at startup.

        Failing here (container restarts, clear error in the logs) is far
        better than silently failing every 30 seconds.
        """
        try:
            await self._with_retry(self._bot.initialize, "getMe")
        except TelegramUnavailableError as exc:
            if isinstance(exc.__cause__, InvalidToken):
                raise TelegramConfigError("BOT_TOKEN was rejected by Telegram") from None
            raise
        await self.verify_permissions()
        logger.info("Telegram bot @%s ready, channel=%s", self._bot.username, self._channel_id)

    async def stop(self) -> None:
        try:
            await self._bot.shutdown()
        except Exception:  # shutdown must never crash the app's own shutdown
            logger.warning("Error while closing Telegram client", exc_info=True)

    async def verify_permissions(self) -> None:
        """Ensure the bot can actually post to CHANNEL_ID."""
        try:
            chat = await self._with_retry(lambda: self._bot.get_chat(self._channel_id), "getChat")
            member = await self._with_retry(
                lambda: self._bot.get_chat_member(chat.id, self._bot.id), "getChatMember"
            )
        except TelegramPublishError as exc:
            raise TelegramConfigError(
                f"Cannot access CHANNEL_ID={self._channel_id}: {exc}. "
                "Check the id and that the bot was added to the channel."
            ) from None

        if chat.type == ChatType.CHANNEL:
            can_post = member.status == ChatMemberStatus.OWNER or (
                member.status == ChatMemberStatus.ADMINISTRATOR
                and bool(getattr(member, "can_post_messages", False))
            )
            if not can_post:
                raise TelegramConfigError(
                    f"Bot is not an admin with 'Post messages' permission in {self._channel_id}"
                )
        elif chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                raise TelegramConfigError(f"Bot is not a member of {self._channel_id}")
        else:
            raise TelegramConfigError(f"{self._channel_id} is a {chat.type} chat, not a channel")

    async def check_connection(self, max_age: float = 60.0) -> bool:
        """Is Telegram reachable? Cached so frequent health probes cost one getMe per minute."""
        now = time.monotonic()
        if self._connection_ok and now - self._connection_ok[0] < max_age:
            return self._connection_ok[1]
        try:
            await asyncio.wait_for(self._bot.get_me(), timeout=5)
            ok = True
        except Exception:
            logger.warning("Telegram connectivity check failed", exc_info=True)
            ok = False
        self._connection_ok = (now, ok)
        return ok

    # ---- sending ---------------------------------------------------------

    async def send_text(self, text: str) -> None:
        await self._with_retry(
            lambda: self._bot.send_message(
                chat_id=self._channel_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            ),
            "sendMessage",
        )

    async def publish_news(self, item: NewsItem) -> None:
        await self.send_text(format_news_message(item))

    async def _with_retry(self, operation: Callable[[], Awaitable[T]], name: str) -> T:
        """Run a Bot API call, retrying transient failures with exponential backoff.

        Note: a TimedOut on sendMessage can mean Telegram delivered the message
        but the response was lost. Retrying can then post it twice; we accept
        that rare duplicate over silently losing news.
        """
        for attempt in range(1, self._max_attempts + 1):
            last_attempt = attempt == self._max_attempts
            try:
                return await operation()
            except RetryAfter as exc:
                wait = _seconds(exc.retry_after)
                if last_attempt or wait > self._max_retry_after:
                    raise TelegramUnavailableError(f"{name}: rate limited for {wait:.0f}s") from exc
                logger.warning("Telegram rate limit on %s, waiting %.0fs", name, wait)
            # BadRequest must come before NetworkError: it is a subclass of it.
            except BadRequest as exc:
                if any(hint in exc.message.lower() for hint in _CONFIG_ERROR_HINTS):
                    raise TelegramUnavailableError(f"{name}: {exc.message}") from exc
                raise MessageRejectedError(f"{name}: {exc.message}") from exc
            except ChatMigrated as exc:
                raise TelegramUnavailableError(
                    f"{name}: chat migrated, set CHANNEL_ID={exc.new_chat_id}"
                ) from exc
            except (Forbidden, InvalidToken) as exc:
                # Permanent until someone fixes the config; retrying won't help.
                raise TelegramUnavailableError(f"{name}: {exc.message}") from exc
            except (TimedOut, NetworkError, TelegramError) as exc:
                if last_attempt:
                    raise TelegramUnavailableError(
                        f"{name} failed after {attempt} attempts: {exc.message}"
                    ) from exc
                wait = self._backoff_delay(attempt)
                logger.warning(
                    "Telegram %s error on %s (attempt %d/%d), retrying in %.1fs",
                    type(exc).__name__, name, attempt, self._max_attempts, wait,
                )
            await self._sleep(wait)
        raise AssertionError("unreachable")  # loop always returns or raises

    def _backoff_delay(self, attempt: int) -> float:
        """1s, 2s, 4s, ... capped, with ±20% jitter so retries don't sync up."""
        delay = min(self._backoff_base * 2 ** (attempt - 1), self._backoff_max)
        return delay * random.uniform(0.8, 1.2)
