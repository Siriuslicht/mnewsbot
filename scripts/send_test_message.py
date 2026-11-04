"""Manual check that the bot can post to your channel.

Usage (from the project root):
    python -m scripts.send_test_message           # plain test message
    python -m scripts.send_test_message --latest  # newest real news item, formatted

Does not touch the database, so it never affects duplicate tracking.
"""

import argparse
import asyncio

from app.config import get_settings
from app.services.marketnews import MarketNewsClient
from app.services.telegram import TelegramPublisher
from app.utils.logger import setup_logging


async def main(latest: bool) -> None:
    settings = get_settings()
    token = settings.bot_token.get_secret_value()
    setup_logging(settings.log_level, secrets=[token])

    # start() also verifies the bot may post to CHANNEL_ID.
    publisher = TelegramPublisher(token, settings.channel_id)
    await publisher.start()
    try:
        if latest:
            client = MarketNewsClient(settings.marketnews_url, settings.http_timeout)
            try:
                items = await client.fetch_latest()
            finally:
                await client.close()
            if not items:
                print("API returned no items")
                return
            await publisher.publish_news(items[-1])  # list is oldest-first
        else:
            await publisher.send_text("✅ Test message from marketnewsfeed-telegram-bot")
        print("Message sent.")
    finally:
        await publisher.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latest", action="store_true", help="post the newest real news item")
    asyncio.run(main(parser.parse_args().latest))
