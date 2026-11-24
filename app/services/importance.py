"""Importance scoring with Claude.

Single responsibility: given news items, return a 1-5 importance score and a
one-word category per item. It knows nothing about Telegram or the database.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

import anthropic
from pydantic import BaseModel

from app.services.marketnews import NewsItem
from app.utils.logger import get_logger

logger = get_logger(__name__)

BATCH_SIZE = 20

SYSTEM_PROMPT = """\
You rate financial news headlines for a Telegram channel whose readers are \
Uzbek investors and broker clients. The channel posts only the most important \
news, so be strict.

Score every headline from 1 to 5:
5 = market-moving breaking news: central bank rate decisions or surprise \
statements (Fed, ECB, BoJ, PBoC, Central Bank of Uzbekistan), major macro \
releases (CPI, jobs report, GDP), sharp moves in oil, gold, major indices or \
Bitcoin, major geopolitical shocks that move markets.
4 = clearly important: central bankers' comments on rates, inflation and other \
macro data, oil, gold and other commodities, OPEC, earnings or guidance of \
major US companies, big moves in major stocks, indices or currencies (USD, \
EUR, RUB, CNY), Bitcoin and Ethereum, tariffs and trade policy, economic news \
about Uzbekistan, Russia, Kazakhstan and other CIS countries.
3 = moderately relevant: smaller companies, sector news, analyst rating \
changes, routine corporate updates.
2 = minor: small caps, niche crypto tokens, local news with little market impact.
1 = irrelevant: lifestyle, personal finance, retirement tips, promotional or \
"follow us" posts.

category: one lowercase English word, for example rates, macro, oil, gold, \
commodities, stocks, earnings, crypto, forex, geopolitics, trade, cis, other.

Return exactly one entry for every id you receive."""


class ImportanceScorerError(Exception):
    """Claude could not score this batch (network, rate limit, API error, bad output)."""


@dataclass(frozen=True)
class ImportanceScore:
    score: int
    category: str


class _ScoredHeadline(BaseModel):
    id: int
    score: int
    category: str


class _ScoredBatch(BaseModel):
    items: list[_ScoredHeadline]


class ImportanceScorer:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        *,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        # The SDK itself retries connection errors, 429 and 5xx twice with
        # backoff; anything still failing surfaces as ImportanceScorerError.
        # `client` is injectable so tests never call the real API.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout, max_retries=2
        )
        self._model = model

    async def score(self, items: Sequence[NewsItem]) -> dict[int, ImportanceScore]:
        """Score items in batches of BATCH_SIZE (one API call per batch).

        Ids missing from Claude's answer are simply absent from the result,
        so the caller can retry them on the next cycle.
        """
        results: dict[int, ImportanceScore] = {}
        for start in range(0, len(items), BATCH_SIZE):
            results.update(await self._score_batch(items[start : start + BATCH_SIZE]))
        return results

    async def _score_batch(self, items: Sequence[NewsItem]) -> dict[int, ImportanceScore]:
        headlines = [{"id": item.hid, "headline": item.text[:500]} for item in items]
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(headlines, ensure_ascii=False)}],
                output_format=_ScoredBatch,
                output_config={"effort": "low"},  # simple classification
            )
        except anthropic.APIError as exc:
            raise ImportanceScorerError(f"{type(exc).__name__}: {exc}") from exc

        usage = response.usage
        logger.info(
            "Claude scored %d headlines: %d input / %d output tokens",
            len(items), usage.input_tokens, usage.output_tokens,
        )
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            raise ImportanceScorerError(f"unusable response (stop_reason={response.stop_reason})")

        wanted = {item.hid for item in items}
        results: dict[int, ImportanceScore] = {}
        for entry in response.parsed_output.items:
            if entry.id in wanted:
                category = (entry.category.strip().lower().split() or ["other"])[0][:32]
                results[entry.id] = ImportanceScore(min(max(entry.score, 1), 5), category)
        if missing := wanted - results.keys():
            logger.warning("Claude returned no score for hids %s; will retry", sorted(missing))
        return results

    async def close(self) -> None:
        await self._client.close()
