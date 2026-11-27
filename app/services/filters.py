"""Pure helpers that decide what may be posted: source exclusion, must-post
keywords, duplicate detection and daily pacing."""

import math
import re
from collections.abc import Collection, Iterable
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


def keyword_pattern(keywords: Collection[str]) -> re.Pattern[str] | None:
    """One case-insensitive regex matching any keyword as a whole word.

    "nvda" matches "$NVDA" and "NVDA:", "dow" does not match "down".
    """
    words = sorted({k.strip() for k in keywords if k.strip()}, key=len, reverse=True)
    if not words:
        return None
    alternatives = "|".join(re.escape(word) for word in words)
    return re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])", re.IGNORECASE)


_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    # Crude plural folding so "gain" and "gains" count as the same word.
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _WORD.findall(text.lower())}


def is_duplicate(text: str, others: Iterable[str], threshold: float = 0.6) -> bool:
    """True if `text` shares most of its words with one of `others`.

    Catches the same headline reposted by several squawk accounts with small
    wording differences ("S&P 500 FUTURES EXTEND GAINS TO 2%").
    """
    words = _words(text)
    if not words:
        return False
    for other in others:
        other_words = _words(other)
        if other_words and len(words & other_words) / len(words | other_words) >= threshold:
            return True
    return False
