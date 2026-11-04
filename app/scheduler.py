"""The polling job: fetch -> filter duplicates -> post -> remember.

This is the only place that combines the three services. Each service stays
independent, so you can swap the news source or the publisher later
(e.g. add a translation step) without touching the others.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.exc import SQLAlchemyError

from app.services.marketnews import MarketNewsClient, MarketNewsError, NewsItem
from app.services.storage import NewsStorage
from app.services.telegram import (
    MessageRejectedError,
    TelegramPublisher,
    TelegramUnavailableError,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


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
    ) -> None:
        self._news_client = news_client
        self._publisher = publisher
        self._storage = storage
        self._post_delay = post_delay
        self._initial_sync = initial_sync
        self._stopping = asyncio.Event()
        self._current_task: asyncio.Task[None] | None = None
        self.status = PollerStatus()

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

        posted = 0
        for item in new_items:
            if self._stopping.is_set():
                logger.info("Shutdown requested, %d items left for next start", len(new_items) - posted)
                break

            try:
                await self._publisher.publish_news(item)
            except MessageRejectedError as exc:
                # Telegram will never accept this message. Record it anyway,
                # otherwise it would block every later item forever.
                logger.error("Skipping hid=%s, rejected by Telegram: %s", item.hid, exc)
            except TelegramUnavailableError as exc:
                # Stop this cycle: the rest would fail too, and stopping keeps
                # posts in order. Unsaved items are retried on the next poll.
                return self._fail(f"Telegram API failure for hid={item.hid}: {exc}", posted)

            # Save only AFTER Telegram accepted (or permanently rejected) it,
            # so transient failures get retried.
            try:
                self._storage.mark_processed(item.hid)
            except SQLAlchemyError:
                # The post went out but wasn't recorded; it may be re-posted
                # next cycle. Stop so we don't repeat this for every item.
                logger.exception("Database error saving hid=%s", item.hid)
                return self._fail("database error", posted)

            posted += 1
            self.status.total_posted += 1
            logger.info("Posted news hid=%s", item.hid)
            await self._pause(self._post_delay)

        return self._succeed(posted)

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
