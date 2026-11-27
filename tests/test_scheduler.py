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


# ---- source exclusion, importance filter, daily cap -------------------------

from datetime import datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from app.services.importance import ImportanceScore, ImportanceScorerError  # noqa: E402

TASHKENT = ZoneInfo("Asia/Tashkent")


class FakeScorer:
    def __init__(self, scores: dict[int, int], error: Exception | None = None) -> None:
        self.scores = scores
        self.error = error
        self.calls: list[list[int]] = []
        self.recent: list[list[str]] = []

    async def score(
        self, items: list[NewsItem], recent_headlines: list[str] = ()  # type: ignore[assignment]
    ) -> dict[int, ImportanceScore]:
        self.calls.append([i.hid for i in items])
        self.recent.append(list(recent_headlines))
        if self.error:
            raise self.error
        return {
            i.hid: ImportanceScore(self.scores[i.hid], "macro") for i in items if i.hid in self.scores
        }


def filtered_poller(news, publisher, storage, scorer=None, hour=12, **kwargs) -> NewsPoller:  # type: ignore[no-untyped-def]
    now = datetime(2026, 11, 24, hour, tzinfo=TASHKENT)
    return NewsPoller(
        news, publisher, storage, post_delay=0, initial_sync=False,
        excluded_accounts=frozenset({"firstsquawk", "marketnews_feed"}),
        scorer=scorer, timezone=TASHKENT, clock=lambda: now, **kwargs,
    )


async def test_blocked_x_accounts_are_never_posted(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    items = [
        make_item(1, link="https://x.com/FirstSquawk/status/2124955452278796607"),
        make_item(2, link="https://x.com/MarketNews_Feed"),
        make_item(3, link="https://x.com/DeItaone/status/5"),
    ]
    publisher = FakePublisher()
    scorer = FakeScorer({3: 5})
    await filtered_poller(FakeNews(items), publisher, storage, scorer).run_once()
    assert publisher.posted == [3]
    assert scorer.calls == [[3]]  # blocked items are not even sent to Claude
    assert storage.filter_new([1, 2]) == set()


async def test_only_important_items_are_posted(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    scorer = FakeScorer({1: 2, 2: 4, 3: 5, 4: 3})
    poller = filtered_poller(FakeNews([make_item(i) for i in (1, 2, 3, 4)]), publisher, storage, scorer)
    assert await poller.run_once() == 2
    assert publisher.posted == [2, 3]  # still oldest first
    assert storage.filter_new([1, 2, 3, 4]) == set()  # low scores are never re-scored


async def test_threshold_is_configurable(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    scorer = FakeScorer({1: 3, 2: 4})
    poller = filtered_poller(
        FakeNews([make_item(1), make_item(2)]), publisher, storage, scorer, min_importance=3
    )
    await poller.run_once()
    assert publisher.posted == [1, 2]


async def test_scoring_failure_keeps_items_for_next_cycle(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    scorer = FakeScorer({1: 5}, error=ImportanceScorerError("rate limited"))
    poller = filtered_poller(FakeNews([make_item(1)]), publisher, storage, scorer)
    assert await poller.run_once() == 0
    assert "Importance scoring failed" in (poller.status.last_error or "")
    assert storage.filter_new([1]) == {1}

    scorer.error = None
    assert await poller.run_once() == 1
    assert publisher.posted == [1]


async def test_scores_are_stored_and_not_rescored(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher(failures={1: TelegramUnavailableError("down")})
    scorer = FakeScorer({1: 5})
    poller = filtered_poller(FakeNews([make_item(1)]), publisher, storage, scorer)
    await poller.run_once()  # scored, but Telegram failed
    publisher.failures.clear()
    await poller.run_once()  # posted using the stored score
    assert publisher.posted == [1]
    assert scorer.calls == [[1]]
    assert storage.get_scores([1]) == {1: ImportanceScore(5, "macro")}


async def test_daily_cap_is_never_exceeded(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    items = [make_item(i) for i in range(1, 8)]
    scorer = FakeScorer({i: 5 for i in range(1, 8)})
    poller = filtered_poller(FakeNews(items), publisher, storage, scorer, hour=23, daily_cap=5)
    assert await poller.run_once() == 5
    assert publisher.posted == [3, 4, 5, 6, 7]  # the newest of equally important items
    assert storage.filter_new(range(1, 8)) == set()  # the rest are dropped, not queued


async def test_pacing_prefers_higher_scores(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    scorer = FakeScorer({1: 4, 2: 5, 3: 4, 4: 4})
    # 00:00 -> pacing allows only the burst (2 posts); the best ones win.
    poller = filtered_poller(
        FakeNews([make_item(i) for i in (1, 2, 3, 4)]), publisher, storage, scorer,
        hour=0, burst=2,
    )
    await poller.run_once()
    assert publisher.posted == [2, 4]  # the score-5 item + the newest score-4 item


async def test_breaking_news_skips_pacing(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    scorer = FakeScorer({1: 5, 2: 5, 3: 4})
    poller = filtered_poller(
        FakeNews([make_item(i) for i in (1, 2, 3)]), publisher, storage, scorer,
        hour=0, burst=0,
    )
    await poller.run_once()
    assert publisher.posted == [1, 2]  # score 5 posts even with no pacing allowance left


# ---- Uzbek translation --------------------------------------------------------

from app.services.translator import Translation, TranslatorError  # noqa: E402


class FakeTranslator:
    def __init__(self, error: Exception | None = None, skip: set[int] | None = None) -> None:
        self.error = error
        self.skip = skip or set()
        self.calls: list[list[int]] = []

    async def translate(self, items: list[NewsItem]) -> dict[int, Translation]:
        self.calls.append([i.hid for i in items])
        if self.error:
            raise self.error
        return {i.hid: Translation(f"Sarlavha {i.hid}", "Tavsif") for i in items if i.hid not in self.skip}


class RecordingPublisher(FakePublisher):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[NewsItem] = []

    async def publish_news(self, item: NewsItem) -> None:
        self.items.append(item)
        await super().publish_news(item)


async def test_only_filtered_items_are_translated(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = RecordingPublisher()
    translator = FakeTranslator()
    poller = filtered_poller(
        FakeNews([make_item(1), make_item(2)]), publisher, storage,
        FakeScorer({1: 2, 2: 5}), translator=translator,
    )
    await poller.run_once()
    assert translator.calls == [[2]]  # the low-score item is never translated
    assert publisher.items[0].title_uz == "Sarlavha 2"
    assert publisher.items[0].description_uz == "Tavsif"


async def test_translation_failure_posts_later(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    translator = FakeTranslator(error=TranslatorError("timeout"))
    poller = filtered_poller(
        FakeNews([make_item(1)]), publisher, storage, FakeScorer({1: 5}), translator=translator
    )
    assert await poller.run_once() == 0
    assert publisher.posted == []  # never posted untranslated
    assert "Translation failed" in (poller.status.last_error or "")
    assert storage.filter_new([1]) == {1}

    translator.error = None
    assert await poller.run_once() == 1
    assert publisher.posted == [1]


async def test_untranslated_item_waits_for_next_cycle(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    translator = FakeTranslator(skip={2})
    poller = filtered_poller(
        FakeNews([make_item(1), make_item(2)]), publisher, storage,
        FakeScorer({1: 5, 2: 5}), translator=translator,
    )
    await poller.run_once()
    assert publisher.posted == [1]
    assert storage.filter_new([2]) == {2}


# ---- scoring in batches (cost saving) ------------------------------------------


async def test_fresh_headlines_wait_for_a_batch(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 11, 24, 12, tzinfo=TASHKENT)
    fresh = now.timestamp() - 60  # published a minute ago
    news = FakeNews([make_item(1, ts=fresh), make_item(2, ts=fresh + 1)])
    publisher = FakePublisher()
    scorer = FakeScorer({1: 5, 2: 5, 3: 5})
    clock = {"now": now}
    poller = NewsPoller(
        news, publisher, storage, post_delay=0, initial_sync=False, scorer=scorer,
        score_batch_size=3, score_max_wait=300, timezone=TASHKENT, clock=lambda: clock["now"],
    )

    assert await poller.run_once() == 0  # 2 waiting, batch of 3 not full yet
    assert scorer.calls == []
    assert storage.filter_new([1, 2]) == {1, 2}  # kept, not dropped

    news.items.append(make_item(3, ts=fresh + 2))
    assert await poller.run_once() == 3  # batch full -> one call for all three
    assert scorer.calls == [[1, 2, 3]]


async def test_lone_headline_is_scored_after_max_wait(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 11, 24, 12, tzinfo=TASHKENT)
    news = FakeNews([make_item(1, ts=now.timestamp() - 60)])
    publisher = FakePublisher()
    scorer = FakeScorer({1: 4})
    clock = {"now": now}
    poller = NewsPoller(
        news, publisher, storage, post_delay=0, initial_sync=False, scorer=scorer,
        score_batch_size=20, score_max_wait=300, timezone=TASHKENT, clock=lambda: clock["now"],
    )
    await poller.run_once()
    assert scorer.calls == []

    clock["now"] = now + timedelta(minutes=5)  # now 6 minutes old
    await poller.run_once()
    assert scorer.calls == [[1]]
    assert publisher.posted == [1]


async def test_recent_posts_are_sent_for_duplicate_check(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    news = FakeNews([make_item(1, text="OIL JUMPS 5%")])
    scorer = FakeScorer({1: 5, 2: 1})
    poller = filtered_poller(news, FakePublisher(), storage, scorer)
    await poller.run_once()
    news.items.append(make_item(2, text="CRUDE OIL SURGES 5%"))
    await poller.run_once()
    assert scorer.recent == [[], ["OIL JUMPS 5%"]]


# ---- priority X accounts -----------------------------------------------------


async def test_priority_accounts_skip_scoring_and_cap(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    items = [
        make_item(1, link="https://x.com/DeItaone/status/1"),
        make_item(2, link="https://x.com/financialjuice/status/2"),
        make_item(3),
    ]
    publisher = FakePublisher()
    scorer = FakeScorer({3: 1})  # priority items get no score at all
    poller = filtered_poller(
        FakeNews(items), publisher, storage, scorer, hour=0, daily_cap=1, burst=0,
        priority_accounts=frozenset({"deitaone", "financialjuice"}),
    )
    assert await poller.run_once() == 2
    assert publisher.posted == [1, 2]
    assert scorer.calls == [[3]]  # only the regular item went to Claude
    assert storage.filter_new([1, 2, 3]) == set()


async def test_priority_posts_do_not_use_the_daily_cap(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    items = [make_item(i, link=f"https://x.com/DeItaone/status/{i}") for i in range(1, 6)]
    items.append(make_item(10))
    publisher = FakePublisher()
    poller = filtered_poller(
        FakeNews(items), publisher, storage, FakeScorer({10: 5}), daily_cap=1, burst=0,
        priority_accounts=frozenset({"deitaone"}),
    )
    assert await poller.run_once() == 6
    assert publisher.posted == [1, 2, 3, 4, 5, 10]


async def test_priority_items_post_while_regular_wait_for_batch(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 11, 24, 12, tzinfo=TASHKENT)
    items = [
        make_item(1, ts=now.timestamp()),
        make_item(2, ts=now.timestamp(), link="https://x.com/financialjuice/status/2"),
    ]
    publisher = FakePublisher()
    scorer = FakeScorer({1: 5})
    poller = filtered_poller(
        FakeNews(items), publisher, storage, scorer, score_batch_size=20, score_max_wait=300,
        priority_accounts=frozenset({"financialjuice"}),
    )
    assert await poller.run_once() == 1
    assert publisher.posted == [2]
    assert scorer.calls == []  # regular item still waiting for a batch
    assert storage.filter_new([1, 2]) == {1}


# ---- must-post keywords ------------------------------------------------------

MUST = ("nvidia", "s&p 500", "nasdaq", "dow")


async def test_must_post_keywords_pass_from_any_source(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    items = [
        make_item(1, text="NVIDIA UP 4.6%", link="https://x.com/FirstSquawk/status/1"),
        make_item(2, text="Markets slowdown worries", link="https://x.com/FirstSquawk/status/2"),
        make_item(3, text="NASDAQ 100 DROPS 2%"),
        make_item(4, text="Small bank merger"),
    ]
    publisher = FakePublisher()
    scorer = FakeScorer({4: 1})
    poller = filtered_poller(
        FakeNews(items), publisher, storage, scorer, daily_cap=1, burst=0, must_post_keywords=MUST
    )
    assert await poller.run_once() == 2
    assert publisher.posted == [1, 3]  # "slowdown" must not match "dow"
    assert scorer.calls == [[4]]  # must-post items skip Claude scoring
    assert storage.filter_new([1, 2, 3, 4]) == set()


async def test_must_post_repeats_are_skipped(storage: NewsStorage, make_item) -> None:  # type: ignore[no-untyped-def]
    items = [
        make_item(1, text="S&P 500 FUTURES EXTEND GAINS TO 2%", link="https://x.com/DeItaone/status/1"),
        make_item(2, text="S&P 500 INDEX FUTURES EXTEND GAIN TO 2%", link="https://x.com/FirstSquawk/status/2"),
        make_item(3, text="NASDAQ 100 EXTENDS DECLINE TO 3%"),
    ]
    publisher = FakePublisher()
    poller = filtered_poller(
        FakeNews(items), publisher, storage,
        priority_accounts=frozenset({"deitaone"}), must_post_keywords=MUST,
    )
    assert await poller.run_once() == 2
    assert publisher.posted == [1, 3]
    assert storage.filter_new([2]) == set()  # the repeat is recorded, never retried
