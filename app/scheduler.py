"""The polling job: fetch -> filter duplicates -> exclude sources -> score
importance -> apply the daily cap -> translate -> post -> remember.

This is the only place that combines the three services. Each service stays
independent, so you can swap the news source or the publisher later
(e.g. add a translation step) without touching the others.
"""

import asyncio
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.exc import SQLAlchemyError

from app.services.filters import (
    day_start,
    is_duplicate,
    is_excluded_source,
    keyword_pattern,
    paced_allowance,
)
from app.services.importance import ImportanceScore, ImportanceScorer, ImportanceScorerError
from app.services.marketnews import MarketNewsClient, MarketNewsError, NewsItem
from app.services.translator import Translator, TranslatorError
from app.services.storage import NewsStorage
from app.services.telegram import (
    MessageRejectedError,
    TelegramPublisher,
    TelegramUnavailableError,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

# How many recently posted headlines Claude sees for duplicate detection.
RECENT_HEADLINES = 30


@dataclass
class PollerStatus:
    """Last-known state, exposed through /ready."""

    last_cycle_at: datetime | None = None  # last cycle that finished without errors
    last_error: str | None = None
    total_posted: int = 0


class NewsPoller:
    def __init__(
        self,
        news_client: MarketNewsClient,
        publisher: TelegramPublisher,
        storage: NewsStorage,
        post_delay: float = 3.0,
        initial_sync: bool = True,
        *,
        excluded_accounts: Collection[str] = (),
        priority_accounts: Collection[str] = (),
        must_post_keywords: Collection[str] = (),
        late_accounts: Collection[str] = (),
        late_hold: float = 0,
        scorer: ImportanceScorer | None = None,
        translator: Translator | None = None,
        score_batch_size: int = 20,
        score_max_wait: float = 0,
        min_importance: int = 4,
        daily_cap: int = 200,
        burst: int = 10,
        timezone: tzinfo = UTC,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._news_client = news_client
        self._publisher = publisher
        self._storage = storage
        self._post_delay = post_delay
        self._initial_sync = initial_sync
        self._excluded_accounts = excluded_accounts
        # Items from these X accounts skip scoring, the daily cap, pacing and
        # the pause between posts: they are posted as soon as they are seen.
        self._priority_accounts = priority_accounts
        # Headlines matching these keywords are posted instantly from any
        # source (even excluded ones), unless they repeat a recent post.
        self._must_post = keyword_pattern(must_post_keywords)
        # Items from these accounts wait `late_hold` seconds before anything is
        # decided, so the same news from another source is posted first and
        # theirs is dropped as a repeat (financialjuice is usually ~2 min early).
        self._late_accounts = late_accounts
        self._late_hold = late_hold
        self._scorer = scorer  # None = post every non-excluded item (still capped)
        self._translator = translator  # None = post the original English text
        # Score when this many headlines are waiting, or when the oldest one
        # was published score_max_wait seconds ago (0 = score immediately).
        self._score_batch_size = score_batch_size
        self._score_max_wait = score_max_wait
        self._min_importance = min_importance
        self._daily_cap = daily_cap
        self._burst = burst
        self._timezone = timezone
        self._clock = clock  # injectable so tests can pick the time of day
        self._stopping = asyncio.Event()
        self._current_task: asyncio.Task[None] | None = None
        self.status = PollerStatus()
        # Recently posted headlines, so repeats of the same story are skipped.
        # Stored in the database too, so a restart doesn't forget them.
        self._recent_posted: deque[str] = deque(maxlen=RECENT_HEADLINES)
        try:
            self._recent_posted.extend(storage.recent_posted_texts(RECENT_HEADLINES))
        except SQLAlchemyError:
            logger.exception("Could not load recent posts; repeat check starts empty")

    async def run_once(self) -> int:
        """Run one polling cycle and return how many items were posted.

        Never raises for expected failures: they are logged and stored in
        `status`, and the next scheduler tick simply tries again.
        """
        try:
            items = await self._news_client.fetch_latest()
            if self._initial_sync and self._storage.is_empty():
                return self._seed(items)
            new_hids = self._storage.filter_new(item.hid for item in items)
        except MarketNewsError as exc:
            return self._fail(f"MarketNewsFeed request failed: {exc}")
        except SQLAlchemyError:
            logger.exception("Database error while checking for duplicates")
            return self._fail("database error")

        new_items = [item for item in items if item.hid in new_hids]
        if not new_items:
            logger.info("No new articles")
            return self._succeed(0)
        logger.info("Found %d new articles", len(new_items))

        now = self._clock()
        held = [
            i for i in new_items
            if is_excluded_source(i.link, self._late_accounts)
            and (now - i.published_at).total_seconds() < self._late_hold
        ]
        if held:
            # Left unprocessed: they come back as new on a later poll.
            logger.info("Holding %d items from late accounts to catch repeats", len(held))
        ready = [i for i in new_items if i not in held]

        priority = [i for i in ready if is_excluded_source(i.link, self._priority_accounts)]
        rest = [i for i in ready if i not in priority]
        must = [i for i in rest if self._must_post and self._must_post.search(i.text)]
        regular = [i for i in rest if i not in must]

        # Priority accounts go out first and never wait for scoring or the budget.
        # Only exact-looking repeats of a recent post are skipped (free check).
        posted = 0
        try:
            priority = self._drop_repeats(priority)
        except SQLAlchemyError:
            logger.exception("Database error while skipping repeats")
            return self._fail("database error")
        if priority:
            logger.info("Posting %d items from priority X accounts instantly", len(priority))
            posted, error = await self._translate_and_post(priority, delay=0, priority=True)
            if error:
                return self._fail(error, posted)

        # Then must-post keywords (Nvidia, major indices, ...) from any source.
        # Claude checks them against recent posts first, because the same news
        # from different squawk accounts is often reworded.
        if must:
            try:
                must = self._drop_repeats(must)
                must = await self._drop_reworded_repeats(must)
            except ImportanceScorerError as exc:
                return self._fail(f"Repeat check failed: {exc}", posted)
            except SQLAlchemyError:
                logger.exception("Database error while checking repeats")
                return self._fail("database error", posted)
            if must:
                logger.info("Posting %d must-post keyword items instantly", len(must))
                more, error = await self._translate_and_post(must, delay=0, priority=True)
                posted += more
                if error:
                    return self._fail(error, posted)

        try:
            to_post = await self._select(regular) if regular else []
        except ImportanceScorerError as exc:
            # Items stay unprocessed and are scored again on the next poll.
            return self._fail(f"Importance scoring failed: {exc}", posted)
        except SQLAlchemyError:
            logger.exception("Database error while filtering news")
            return self._fail("database error", posted)

        more, error = await self._translate_and_post(to_post, delay=self._post_delay)
        posted += more
        if error:
            return self._fail(error, posted)
        return self._succeed(posted)

    def _drop_repeats(self, items: list[NewsItem]) -> list[NewsItem]:
        """Skip headlines that repeat a recent post or an earlier one in this list."""
        keep: list[NewsItem] = []
        repeats: list[NewsItem] = []
        for item in items:
            seen = [*self._recent_posted, *(k.text for k in keep)]
            (repeats if is_duplicate(item.text, seen) else keep).append(item)
        if repeats:
            self._storage.mark_many(i.hid for i in repeats)
            logger.info("Skipped %d items repeating a recent post", len(repeats))
        return keep

    async def _drop_reworded_repeats(self, items: list[NewsItem]) -> list[NewsItem]:
        """Ask Claude (the importance scorer) whether these repeat a recent post.

        Score 1 means duplicate or irrelevant: skipped. Any other score is
        posted, ignoring the threshold and the daily cap. Scored right away,
        without waiting for a batch, so they still go out promptly.
        """
        if self._scorer is None:
            return items
        scores = self._storage.get_scores(i.hid for i in items)
        unscored = [i for i in items if i.hid not in scores]
        if unscored:
            fresh = await self._scorer.score(unscored, list(self._recent_posted))
            self._storage.save_scores(fresh)
            scores.update(fresh)
        items = [i for i in items if i.hid in scores]  # missing ones retry next poll
        repeats = [i for i in items if scores[i.hid].score <= 1]
        if repeats:
            self._storage.mark_many(i.hid for i in repeats)
            logger.info("Claude marked %d must-post items as repeats", len(repeats))
        return [i for i in items if i not in repeats]

    async def _translate_and_post(
        self, to_post: list[NewsItem], *, delay: float, priority: bool = False
    ) -> tuple[int, str | None]:
        """Translate and publish items in order. Returns (posted, error message or None)."""
        if self._translator is not None and to_post:
            try:
                translations = await self._translator.translate(to_post)
            except TranslatorError as exc:
                # Never post untranslated: the items stay unprocessed and are
                # translated and posted on the next poll.
                return 0, f"Translation failed: {exc}"
            to_post = [
                item.model_copy(
                    update={
                        "title_uz": t.title,
                        "description_uz": t.description,
                        "tags": list(t.tags),
                    }
                )
                for item in to_post
                if (t := translations.get(item.hid))
            ]

        posted = 0
        for item in to_post:
            if self._stopping.is_set():
                logger.info("Shutdown requested, %d items left for next start", len(to_post) - posted)
                break

            delivered = False
            try:
                await self._publisher.publish_news(item)
                delivered = True
            except MessageRejectedError as exc:
                # Telegram will never accept this message. Record it anyway,
                # otherwise it would block every later item forever.
                logger.error("Skipping hid=%s, rejected by Telegram: %s", item.hid, exc)
            except TelegramUnavailableError as exc:
                # Stop this cycle: the rest would fail too, and stopping keeps
                # posts in order. Unsaved items are retried on the next poll.
                return posted, f"Telegram API failure for hid={item.hid}: {exc}"

            # Save only AFTER Telegram accepted (or permanently rejected) it,
            # so transient failures get retried.
            try:
                if delivered:
                    self._storage.mark_posted(item.hid, priority=priority)
                    self._storage.save_recent_post(item.hid, item.text)
                self._storage.mark_processed(item.hid)
            except SQLAlchemyError:
                # The post went out but wasn't recorded; it may be re-posted
                # next cycle. Stop so we don't repeat this for every item.
                logger.exception("Database error saving hid=%s", item.hid)
                return posted, "database error"

            posted += 1
            self.status.total_posted += 1
            self._recent_posted.append(item.text)
            logger.info("Posted news hid=%s%s", item.hid, " (priority)" if priority else "")
            await self._pause(delay)

        return posted, None

    async def _select(self, items: list[NewsItem]) -> list[NewsItem]:
        """Decide which new items to post, oldest first.

        Excluded, low-importance and over-budget items are recorded as
        processed so they are never looked at again. Items Claude has not
        scored yet stay unprocessed and are retried on the next poll.
        """
        excluded = [i for i in items if is_excluded_source(i.link, self._excluded_accounts)]
        if excluded:
            self._storage.mark_many(i.hid for i in excluded)
            logger.info("Excluded %d items from blocked X accounts", len(excluded))
        candidates = [i for i in items if i not in excluded]

        scores: dict[int, ImportanceScore] = {}
        if self._scorer is not None and candidates:
            scores = self._storage.get_scores(i.hid for i in candidates)
            unscored = [i for i in candidates if i.hid not in scores]
            if unscored and not self._batch_ready(unscored):
                # Cost saving: wait until enough headlines pile up (or the
                # oldest has waited long enough) and score them in one call.
                logger.info("Waiting to score %d headlines in one batch", len(unscored))
                unscored = []
            if unscored:
                fresh = await self._scorer.score(unscored, list(self._recent_posted))
                self._storage.save_scores(fresh)
                scores.update(fresh)
            candidates = [i for i in candidates if i.hid in scores]
            low = [i for i in candidates if scores[i.hid].score < self._min_importance]
            if low:
                self._storage.mark_many(i.hid for i in low)
                logger.info("Skipped %d items below importance %d", len(low), self._min_importance)
            candidates = [i for i in candidates if i not in low]

        chosen = self._apply_budget(candidates, scores)
        for item in chosen:
            s = scores.get(item.hid)
            logger.info(
                "Will post hid=%s%s", item.hid, f" score={s.score} ({s.category})" if s else ""
            )
        return chosen

    def _batch_ready(self, unscored: list[NewsItem]) -> bool:
        if len(unscored) >= self._score_batch_size:
            return True
        oldest = min(item.published_at for item in unscored)
        return (self._clock() - oldest).total_seconds() >= self._score_max_wait

    def _apply_budget(
        self, items: list[NewsItem], scores: dict[int, ImportanceScore]
    ) -> list[NewsItem]:
        """Keep the most important items that fit today's cap and pacing.

        Score-5 (breaking) items ignore pacing so they are never held back by
        a quiet morning; they still respect the hard daily cap.
        """
        now_local = self._clock().astimezone(self._timezone)
        posted_today = self._storage.count_posted_since(day_start(now_local).astimezone(UTC))
        cap_left = max(self._daily_cap - posted_today, 0)
        paced_left = max(paced_allowance(now_local, self._daily_cap, self._burst) - posted_today, 0)

        def score_of(item: NewsItem) -> int:
            return scores[item.hid].score if item.hid in scores else 0

        # Highest score first; among equal scores the newest wins.
        ranked = sorted(items, key=lambda i: (score_of(i), i.ts), reverse=True)
        keep: set[int] = set()
        for item in ranked:
            if len(keep) >= cap_left:
                break
            if score_of(item) >= 5 or len(keep) < paced_left:
                keep.add(item.hid)

        dropped = [i for i in items if i.hid not in keep]
        if dropped:
            self._storage.mark_many(i.hid for i in dropped)
            logger.info(
                "Daily budget: skipped %d items (%d posted today, cap %d)",
                len(dropped), posted_today, self._daily_cap,
            )
        return [i for i in items if i.hid in keep]

    def _seed(self, items: list[NewsItem]) -> int:
        """First run: remember the current batch WITHOUT posting it."""
        stored = self._storage.mark_many(item.hid for item in items)
        logger.info(
            "Initial sync: stored %d existing news ids without posting. "
            "Only news published from now on will be posted.",
            stored,
        )
        return self._succeed(0)

    def _succeed(self, posted: int) -> int:
        self.status.last_cycle_at = datetime.now(UTC)
        self.status.last_error = None
        return posted

    def _fail(self, message: str, posted: int = 0) -> int:
        logger.error(message)
        self.status.last_error = message
        return posted

    async def _pause(self, seconds: float) -> None:
        """Sleep between posts, but wake up immediately on shutdown."""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    # ---- scheduler integration ------------------------------------------

    async def safe_run(self) -> None:
        """Scheduler entry point: tracks the running task and never leaks exceptions."""
        if self._stopping.is_set():
            return
        self._current_task = asyncio.current_task()
        try:
            await self.run_once()
        except asyncio.CancelledError:
            # Only happens when shutdown cancels a cycle that overran
            # SHUTDOWN_TIMEOUT. Swallowed on purpose: no traceback noise.
            logger.info("Polling cycle cancelled during shutdown")
        except Exception:
            logger.exception("Unexpected error in polling cycle")
            self.status.last_error = "unexpected error"
        finally:
            self._current_task = None

    async def stop(self, timeout: float) -> None:
        """Graceful stop: let the current post finish, then cancel if it takes too long."""
        self._stopping.set()
        task = self._current_task
        if task is None or task.done():
            return
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            logger.warning("Polling cycle still running after %.0fs, cancelling it", timeout)
            task.cancel()
            await asyncio.wait({task})


def create_scheduler(poller: NewsPoller, interval_seconds: int) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=UTC)
    scheduler.add_job(
        poller.safe_run,
        trigger="interval",
        seconds=interval_seconds,
        id="poll_market_news",
        # A cycle with many posts can take longer than the interval.
        # max_instances=1 prevents overlapping cycles (which could double-post);
        # coalesce=True collapses missed ticks into a single run.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=interval_seconds,
        next_run_time=datetime.now(UTC),  # run immediately, don't wait one interval
    )
    return scheduler
