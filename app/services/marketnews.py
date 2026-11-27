"""MarketNewsFeed API client.

Single responsibility: call the API, validate the response, return news objects.
It knows nothing about Telegram or the database.
"""

from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.utils.logger import get_logger

logger = get_logger(__name__)


class MarketNewsError(Exception):
    """Raised when the API is unreachable or returns something unusable."""


class AssetTag(BaseModel):
    """A stock, index or other asset the news is about, shown as a hashtag line."""

    symbol: str  # e.g. NVDA, SPX; letters and digits only
    country: str = ""  # ISO 3166 two-letter code for the flag, or "" for none
    change_pct: float | None = None  # move stated in the news, if any
    direction: Literal["up", "down", "none"] = "none"  # rising or falling, per the news


class NewsItem(BaseModel):
    """One news item. Only the fields we use are declared; extras are ignored."""

    model_config = ConfigDict(str_strip_whitespace=True)

    hid: int = Field(gt=0)
    ts: float
    text: str = Field(min_length=1)
    link: str | None = None
    chan: str | None = None
    dir: str | None = None
    cat: str | None = None
    # Filled in by the translator, never by the API.
    title_uz: str | None = Field(None, exclude=True)
    description_uz: str | None = Field(None, exclude=True)
    tags: list[AssetTag] = Field(default_factory=list, exclude=True)

    @property
    def published_at(self) -> datetime:
        return datetime.fromtimestamp(self.ts, tz=UTC)


class MarketNewsClient:
    def __init__(
        self,
        url: str,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        # One shared client = connection pooling across polls.
        # `transport` lets tests plug in httpx.MockTransport instead of the network.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "marketnewsfeed-telegram-bot/1.0"},
            transport=transport,
            follow_redirects=True,
        )

    async def fetch_latest(self) -> list[NewsItem]:
        """Fetch the latest news, oldest first.

        Raises MarketNewsError for network/HTTP/format problems. Individual
        malformed items are skipped (logged) instead of failing the whole batch.
        """
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise MarketNewsError(f"API returned HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise MarketNewsError(f"API unavailable: {exc!r}") from exc
        except ValueError as exc:  # response body is not JSON
            raise MarketNewsError("API returned invalid JSON") from exc

        # The API wraps items as {"items": [...]}; accept a bare list too.
        raw_items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list):
            raise MarketNewsError("Unexpected response shape: no 'items' list")

        # Keyed by hid so a duplicated entry in one response is posted once.
        by_hid: dict[int, NewsItem] = {}
        for raw in raw_items:
            try:
                item = NewsItem.model_validate(raw)
            except ValidationError as exc:
                logger.warning("Skipping invalid news item: %s", exc.errors()[:1])
                continue
            by_hid[item.hid] = item

        # The API returns newest first; we post oldest first so the channel
        # reads in chronological order.
        items = sorted(by_hid.values(), key=lambda item: (item.ts, item.hid))
        logger.info("Fetched %d news items", len(items))
        return items

    async def close(self) -> None:
        await self._client.aclose()
