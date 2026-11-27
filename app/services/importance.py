"""Importance scoring with Claude.

Single responsibility: given news items, return a 1-5 importance score and a
one-word category per item. It knows nothing about Telegram or the database.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

import anthropic
from pydantic import BaseModel, ValidationError

from app.services.marketnews import NewsItem
from app.utils.logger import get_logger

logger = get_logger(__name__)

BATCH_SIZE = 20
# Recent posts sent for the repeat check; fewer = cheaper calls.
RECENT_FOR_CLAUDE = 10

SYSTEM_PROMPT = """\
You rate financial news headlines for a Telegram channel whose readers are \
Uzbek investors and broker clients who trade mainly on the stock market. The \
channel's focus is the stock market: major US companies, big moves in major \
stocks, stock indices and currencies, and the news that moves them. Be strict: \
typically only about one headline in five deserves a 4 or 5.

Score every headline in "headlines" from 1 to 5:
5 = market-moving breaking news: big moves in the S&P 500, Nasdaq, Dow or \
other major indices, sharp moves in mega-cap stocks (Nvidia, Apple, Microsoft, \
Alphabet, Amazon, Meta, Tesla), central bank rate decisions or surprise \
statements (Fed, ECB, BoJ, PBoC, Central Bank of Uzbekistan), major macro \
releases (US CPI, jobs report, GDP), major geopolitical shocks whose market \
impact the headline itself states (oil spikes, stocks sell off, sanctions on \
trade).
4 = clearly important and new for stock investors: earnings, guidance, deals \
and big moves of major US companies, index and futures moves, big moves in \
major currencies (USD, EUR, RUB, CNY), central bank leaders on rates, key \
inflation and macro data, sharp moves in oil and gold, tariffs and trade \
policy that hit stocks, economic news about Uzbekistan and other CIS countries.
3 = relevant but routine: smaller companies, sector news, analyst ratings and \
price targets, routine or weekly data (weekly ADP, SOFR, minor surveys), \
ordinary commodity and crypto news, and further remarks by an official who \
already made the main point.
2 = minor: small caps, niche crypto tokens, bond auctions, local news with \
little market impact.
Not financial news, score 1 or 2 however big the story: politics, war, \
military, crime, hacking, disasters, sports and celebrities, unless the \
headline itself is about markets, prices, companies, trade or the economy.
1 = irrelevant: lifestyle, personal finance, retirement tips, promotional or \
"follow us" posts, and DUPLICATES.

Duplicates: if a headline reports the same news as one in "recently_posted", \
or as another headline in this batch, it is a duplicate and gets 1. Within a \
batch keep the most informative version and score only that one normally. \
Headlines about the same data release, decision or event are duplicates even \
when worded differently, for example "ECB survey: 1-yr inflation expectations \
rise to 3.5%" and "Euro area 1-year CPI expectations rise to 3.5%".

category: one lowercase English word, for example rates, macro, oil, gold, \
commodities, stocks, earnings, crypto, forex, geopolitics, trade, cis, other.

Return exactly one entry for every id in "headlines"."""


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

    async def score(
        self, items: Sequence[NewsItem], recent_headlines: Sequence[str] = ()
    ) -> dict[int, ImportanceScore]:
        """Score items in batches of BATCH_SIZE (one API call per batch).

        `recent_headlines` are headlines posted recently; repeats of the same
        news are scored 1 so the channel doesn't post one story many times.

        Ids missing from Claude's answer are simply absent from the result,
        so the caller can retry them on the next cycle.
        """
        results: dict[int, ImportanceScore] = {}
        for start in range(0, len(items), BATCH_SIZE):
            batch = items[start : start + BATCH_SIZE]
            results.update(await self._score_batch(batch, recent_headlines))
        return results

    async def _score_batch(
        self, items: Sequence[NewsItem], recent_headlines: Sequence[str]
    ) -> dict[int, ImportanceScore]:
        payload = {
            "recently_posted": [text[:100] for text in list(recent_headlines)[-RECENT_FOR_CLAUDE:]],
            "headlines": [{"id": item.hid, "headline": item.text[:500]} for item in items],
        }
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=60 * len(items) + 200,
                # Cached: the same instructions go with every call, so after the
                # first call they cost about a tenth (Haiku caches 512+ tokens).
                system=[
                    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                output_format=_ScoredBatch,
                # Simple classification: no extended thinking (saves output tokens).
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
            )
        except anthropic.APIError as exc:
            raise ImportanceScorerError(f"{type(exc).__name__}: {exc}") from exc
        except ValidationError as exc:
            # Claude's answer wasn't valid JSON for the schema (e.g. cut off).
            raise ImportanceScorerError(f"unusable response: {exc.errors()[:1]}") from exc

        usage = response.usage
        logger.info(
            "Claude scored %d headlines: %d input (%d cached) / %d output tokens",
            len(items), usage.input_tokens,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            usage.output_tokens,
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
