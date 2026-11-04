import asyncio

from app.scheduler import NewsPoller, create_scheduler
from app.services.marketnews import MarketNewsError, NewsItem
from app.services.storage import NewsStorage
from app.services.telegram import MessageRejectedError, TelegramUnavailableError


class FakeNews:
    def __init__(self, items: list[NewsItem] | None = None, error: Exception | None = None) -> None:
        self.items = items or []
        self.error = error

    async def fetch_latest(self) -> list[NewsItem]:
        if self.error:
            raise self.error
        return list(self.items)


class FakePublisher:
    """Records posts; `failures` maps hid -> exception to raise for that item."""

    def __init__(self, failures: dict[int, Exception] | None = None, delay: float = 0) -> None:
        self.posted: list[int] = []
        self.failures = failures or {}
        self.delay = delay

    async def publish_news(self, item: NewsItem) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if item.hid in self.failures:
            raise self.failures[item.hid]
        self.posted.append(item.hid)


def make_poller(news, publisher, storage, initial_sync=False) -> NewsPoller:  # type: ignore[no-untyped-def]
    return NewsPoller(news, publisher, storage, post_delay=0, initial_sync=initial_sync)


async def test_initial_sync_stores_without_posting(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    news = FakeNews([make_item(1), make_item(2), make_item(3)])
    publisher = FakePublisher()
    poller = make_poller(news, publisher, storage, initial_sync=True)

    assert await poller.run_once() == 0
    assert publisher.posted == []
    assert storage.filter_new([1, 2, 3]) == set()

    # Afterwards only genuinely new news is posted.
    news.items.append(make_item(4))
    assert await poller.run_once() == 1
    assert publisher.posted == [4]


async def test_initial_sync_only_when_database_empty(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    storage.mark_processed(1)  # existing deployment
    publisher = FakePublisher()
    poller = make_poller(FakeNews([make_item(1), make_item(2)]), publisher, storage, initial_sync=True)
    assert await poller.run_once() == 1
    assert publisher.posted == [2]


async def test_initial_sync_disabled_posts_everything(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    poller = make_poller(FakeNews([make_item(1), make_item(2)]), publisher, storage)
    assert await poller.run_once() == 2


async def test_duplicates_are_never_reposted(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    poller = make_poller(FakeNews([make_item(1), make_item(2)]), publisher, storage)
    await poller.run_once()
    await poller.run_once()
    # "Restart": brand-new poller, same database.
    await make_poller(FakeNews([make_item(1), make_item(2)]), publisher, storage).run_once()
    assert publisher.posted == [1, 2]


async def test_telegram_outage_stops_cycle_and_retries_later(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    items = [make_item(1), make_item(2), make_item(3)]
    publisher = FakePublisher(failures={2: TelegramUnavailableError("down")})
    poller = make_poller(FakeNews(items), publisher, storage)

    assert await poller.run_once() == 1
    assert publisher.posted == [1]
    assert storage.filter_new([2, 3]) == {2, 3}  # not saved -> retried
    assert poller.status.last_error is not None

    publisher.failures.clear()
    assert await poller.run_once() == 2
    assert publisher.posted == [1, 2, 3]
    assert poller.status.last_error is None


async def test_rejected_message_is_skipped_not_blocking(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher(failures={2: MessageRejectedError("bad")})
    poller = make_poller(FakeNews([make_item(1), make_item(2), make_item(3)]), publisher, storage)
    await poller.run_once()
    assert publisher.posted == [1, 3]
    assert storage.filter_new([2]) == set()  # recorded so it never blocks again


async def test_api_failure_is_handled(storage: NewsStorage) -> None:
    poller = make_poller(FakeNews(error=MarketNewsError("down")), FakePublisher(), storage)
    assert await poller.run_once() == 0
    assert "MarketNewsFeed" in (poller.status.last_error or "")


async def test_safe_run_swallows_unexpected_errors(storage: NewsStorage) -> None:
    poller = make_poller(FakeNews(error=RuntimeError("bug")), FakePublisher(), storage)
    await poller.safe_run()  # must not raise
    assert poller.status.last_error == "unexpected error"


async def test_stop_waits_for_current_post(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher(delay=0.05)
    poller = make_poller(FakeNews([make_item(1), make_item(2), make_item(3)]), publisher, storage)
    task = asyncio.create_task(poller.safe_run())
    await asyncio.sleep(0.01)
    await poller.stop(timeout=5)
    assert task.done() and not task.cancelled()
    assert publisher.posted == [1]  # finished the in-flight post, then stopped
    assert storage.filter_new([1]) == set()


async def test_stop_cancels_cleanly_after_timeout(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher(delay=10)
    poller = make_poller(FakeNews([make_item(1)]), publisher, storage)
    task = asyncio.create_task(poller.safe_run())
    await asyncio.sleep(0.01)
    await poller.stop(timeout=0.05)
    assert task.done()
    assert task.exception() is None  # no CancelledError leaks out
    assert storage.filter_new([1]) == {1}


async def test_scheduler_prevents_overlap(storage: NewsStorage) -> None:
    poller = make_poller(FakeNews(), FakePublisher(), storage)
    job = create_scheduler(poller, 30).get_job("poll_market_news")
    assert job.max_instances == 1
    assert job.coalesce is True
