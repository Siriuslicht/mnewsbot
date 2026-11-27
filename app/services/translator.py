"""English -> Uzbek (Latin script) translation with Claude.

Single responsibility: translate news items. It knows nothing about Telegram
or the database. Only items that already passed the importance filter are
translated, which keeps the cost down.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

import anthropic
from pydantic import BaseModel, Field

from app.services.marketnews import AssetTag, NewsItem
from app.utils.logger import get_logger

logger = get_logger(__name__)

BATCH_SIZE = 10

SYSTEM_PROMPT = """\
You translate English financial news into Uzbek (Latin script) for a Telegram \
channel read by Uzbek investors and broker clients.

For every item return:
- title: the headline in Uzbek.
- description: the rest of the text in Uzbek (one or two short sentences), or \
an empty string if the item is only a headline.
- tags: the stocks, indices, currencies or commodities the news is mainly \
about, at most 6, most important first, or an empty list if it is not about \
specific assets. For each tag:
  - symbol: the stock ticker (NVDA, AAPL, MSFT); for indices SPX, NDX, DJI, \
RUT; for currencies EURUSD, USDRUB, DXY; for commodities GOLD, OIL, SILVER; \
for crypto BTC, ETH; for a private company a short uppercase name (ANTHROPIC, \
SPACEX). Uppercase letters and digits only.
  - country: the two-letter ISO country code of the company or index (US, CN, \
JP, DE, UZ), also for private companies (SpaceX is US), or an empty string \
for currencies, commodities and crypto.
  - change_pct: the percentage move the news states for that asset, as a \
number (4.6 or -2.1), or null if no percentage is stated. Never guess or \
calculate a number.

Rules:
- Write natural, concise Uzbek in normal sentence case, never ALL CAPS, even \
when the source is in capitals.
- Keep tickers ($NVDA, BTC, S&P 500), company names, person names, \
organisation abbreviations (Fed, ECB, OPEC, WSJ) and numbers unchanged in value.
- Use Uzbek number formatting: decimal comma (2,5%) and a space as thousands \
separator ($60 000).
- Translate meaning only. Add nothing, explain nothing, drop hashtags and \
emojis, and never write "JUST IN"-style prefixes.

Return exactly one entry for every id you receive."""


class TranslatorError(Exception):
    """Claude could not translate this batch (network, rate limit, API error, bad output)."""


@dataclass(frozen=True)
class Translation:
    title: str
    description: str
    tags: tuple[AssetTag, ...] = ()


class _TranslatedItem(BaseModel):
    id: int
    title: str
    description: str
    tags: list[AssetTag] = Field(default_factory=list)


class _TranslatedBatch(BaseModel):
    items: list[_TranslatedItem]


class Translator:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        *,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        # The SDK retries connection errors, 429 and 5xx twice with backoff.
        # `client` is injectable so tests never call the real API.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout, max_retries=2
        )
        self._model = model

    async def translate(self, items: Sequence[NewsItem]) -> dict[int, Translation]:
        """Translate items in batches. Ids missing from the answer are left out,
        so the caller can retry them on the next cycle."""
        results: dict[int, Translation] = {}
        for start in range(0, len(items), BATCH_SIZE):
            results.update(await self._translate_batch(items[start : start + BATCH_SIZE]))
        return results

    async def _translate_batch(self, items: Sequence[NewsItem]) -> dict[int, Translation]:
        payload = [{"id": item.hid, "text": item.text[:1500]} for item in items]
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=400 * len(items) + 200,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                output_format=_TranslatedBatch,
                # Thinking off: translation doesn't need it and it tripled output tokens.
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
            )
        except anthropic.APIError as exc:
            raise TranslatorError(f"{type(exc).__name__}: {exc}") from exc

        usage = response.usage
        logger.info(
            "Claude translated %d items: %d input / %d output tokens",
            len(items), usage.input_tokens, usage.output_tokens,
        )
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            raise TranslatorError(f"unusable response (stop_reason={response.stop_reason})")

        wanted = {item.hid for item in items}
        results = {
            entry.id: Translation(
                entry.title.strip(), entry.description.strip(), _clean_tags(entry.tags)
            )
            for entry in response.parsed_output.items
            if entry.id in wanted and entry.title.strip()
        }
        if missing := wanted - results.keys():
            logger.warning("Claude returned no translation for hids %s; will retry", sorted(missing))
        return results

    async def close(self) -> None:
        await self._client.close()


_SYMBOL = re.compile(r"^[A-Z0-9]{1,12}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
MAX_TAGS = 6


def _clean_tags(tags: Sequence[AssetTag]) -> tuple[AssetTag, ...]:
    """Drop malformed or repeated tags so a bad answer never breaks a post."""
    seen: set[str] = set()
    cleaned: list[AssetTag] = []
    for tag in tags:
        symbol = tag.symbol.strip().lstrip("#$").upper()
        if not _SYMBOL.match(symbol) or symbol in seen:
            continue
        country = tag.country.strip().upper()
        seen.add(symbol)
        cleaned.append(
            AssetTag(
                symbol=symbol,
                country=country if _COUNTRY.match(country) else "",
                change_pct=tag.change_pct,
            )
        )
    return tuple(cleaned[:MAX_TAGS])
