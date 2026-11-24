"""Pure helpers that decide what may be posted: source exclusion and daily pacing."""

import math
from collections.abc import Collection
from datetime import datetime, timedelta
from urllib.parse import urlparse

_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}


def is_excluded_source(link: str | None, excluded_accounts: Collection[str]) -> bool:
    """True if the link points to one of the excluded X accounts (case-insensitive).

    Matches both https://x.com/FirstSquawk/status/123 and https://x.com/MarketNews_Feed.
    """
    if not link or not excluded_accounts:
        return False
    parsed = urlparse(link.strip())
    if parsed.netloc.lower() not in _X_HOSTS:
        return False
    account = parsed.path.strip("/").split("/", 1)[0].lower()
    return account in excluded_accounts


def day_start(now_local: datetime) -> datetime:
    return now_local.replace(hour=0, minute=0, second=0, microsecond=0)


def paced_allowance(now_local: datetime, daily_cap: int, burst: int) -> int:
    """How many posts may have gone out by `now_local` today.

    The cap is spread evenly over the day (about 8 per hour for 200/day).
    Unused allowance from quiet hours carries over, and `burst` lets a few
    extra posts through when a lot happens at once.
    """
    elapsed = (now_local - day_start(now_local)) / timedelta(days=1)
    return min(daily_cap, math.ceil(daily_cap * elapsed) + burst)
