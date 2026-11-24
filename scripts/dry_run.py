"""Dry run: one full polling cycle WITHOUT posting to Telegram.

Usage (from the project root):
    python -m scripts.dry_run              # real feed + real Claude scoring
    python -m scripts.dry_run --no-claude  # skip Claude (no API cost, English)
    python -m scripts.dry_run --show 5     # print the first 5 formatted posts

Uses a throwaway temporary database, so it never touches the real one and
every item in the current feed counts as new. Claude scoring costs a fraction
of a cent per run.
"""

import argparse
import asyncio
import os
import tempfile
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.marketnews import NewsItem


class PrintPublisher:
    """Stands in for Telegram: collects messages instead of sending them."""

    def __init__(self) -> None:
        self.messages: list[tuple[NewsItem, str]] = []

    async def publish_news(self, item: NewsItem) -> None:
        from app.services.telegram import format_news_message

        self.messages.append((item, format_news_message(item)))


async def main(use_claude: bool, show: int) -> None:
    if not use_claude:
        os.environ["IMPORTANCE_FILTER"] = "false"
        os.environ["TRANSLATE_UZ"] = "false"

    from app.config import get_settings
    from app.database.database import create_db_engine, create_session_factory, init_db
    from app.scheduler import NewsPoller
    from app.services.filters import is_excluded_source
    from app.services.importance import ImportanceScorer
    from app.services.marketnews import MarketNewsClient
    from app.services.storage import NewsStorage
    from app.services.translator import Translator
    from app.utils.logger import setup_logging

    settings = get_settings()
    secrets = [settings.bot_token.get_secret_value()]
    if settings.anthropic_api_key:
        secrets.append(settings.anthropic_api_key.get_secret_value())
    setup_logging("WARNING", secrets=secrets)

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_db_engine(f"sqlite:///{Path(tmp) / 'dry_run.db'}")
        init_db(engine)
        storage = NewsStorage(create_session_factory(engine))
        news = MarketNewsClient(settings.marketnews_url, settings.http_timeout)
        scorer = None
        if settings.importance_filter and settings.anthropic_api_key:
            scorer = ImportanceScorer(
                settings.anthropic_api_key.get_secret_value(),
                settings.claude_model,
                settings.claude_timeout,
            )
        translator = None
        if settings.translate_uz and settings.anthropic_api_key:
            translator = Translator(
                settings.anthropic_api_key.get_secret_value(),
                settings.claude_model,
                settings.claude_timeout,
            )
        publisher = PrintPublisher()
        poller = NewsPoller(
            news, publisher, storage, post_delay=0, initial_sync=False,  # type: ignore[arg-type]
            excluded_accounts=settings.excluded_accounts,
            scorer=scorer,
            translator=translator,
            min_importance=settings.min_importance,
            daily_cap=settings.daily_post_cap,
            burst=settings.post_burst,
            timezone=ZoneInfo(settings.bot_timezone),
        )
        try:
            items = await news.fetch_latest()
            news.fetch_latest = lambda: _same(items)  # type: ignore[method-assign]
            await poller.run_once()
            scores = storage.get_scores(i.hid for i in items)
        finally:
            await news.close()
            for claude in (scorer, translator):
                if claude:
                    await claude.close()
            engine.dispose()

    posted = {item.hid for item, _ in publisher.messages}
    print(f"\nFeed: {len(items)} items | model: {settings.claude_model if scorer else 'OFF'} | "
          f"min score {settings.min_importance} | cap {settings.daily_post_cap}/day\n")
    print(f"{'hid':>7}  {'decision':<9} {'score':>5}  {'category':<11} headline")
    for item in reversed(items):  # newest first
        s = scores.get(item.hid)
        if is_excluded_source(item.link, settings.excluded_accounts):
            decision = "EXCLUDED"
        elif item.hid in posted:
            decision = "POST"
        elif scorer and s is None:
            decision = "UNSCORED"
        else:
            decision = "skip"
        print(f"{item.hid:>7}  {decision:<9} {s.score if s else '-':>5}  "
              f"{s.category if s else '-':<11} {item.text[:80].replace(chr(10), ' ')}")

    counts = Counter(s.score for s in scores.values())
    print(f"\nWould post {len(posted)} of {len(items)}. "
          f"Score distribution: {dict(sorted(counts.items()))}")
    if poller.status.last_error:
        print(f"ERROR: {poller.status.last_error}")
    for item, message in publisher.messages[:show]:
        print(f"\n----- post preview (hid={item.hid}) -----\n{message}")


async def _same(items: list[NewsItem]) -> list[NewsItem]:
    return items


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-claude", action="store_true", help="skip importance scoring")
    parser.add_argument("--show", type=int, default=2, help="formatted posts to print")
    args = parser.parse_args()
    asyncio.run(main(not args.no_claude, args.show))
