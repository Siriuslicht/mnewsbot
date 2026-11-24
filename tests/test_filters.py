from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.filters import is_excluded_source, paced_allowance

BLOCKED = frozenset({"firstsquawk", "marketnews_feed"})
TASHKENT = ZoneInfo("Asia/Tashkent")


@pytest.mark.parametrize(
    "link",
    [
        "https://x.com/FirstSquawk/status/2124955452278796607",
        "https://x.com/MarketNews_Feed",
        "https://twitter.com/firstsquawk/status/1",
        "https://www.x.com/MARKETNEWS_FEED/",
    ],
)
def test_blocked_accounts_are_excluded(link: str) -> None:
    assert is_excluded_source(link, BLOCKED)


@pytest.mark.parametrize(
    "link",
    [
        "https://x.com/DeItaone/status/1",
        "https://www.tradingview.com/news/FirstSquawk-story/",
        "https://x.com/FirstSquawkFan/status/1",
        None,
        "",
    ],
)
def test_other_sources_pass(link: str | None) -> None:
    assert not is_excluded_source(link, BLOCKED)


def test_pacing_spreads_cap_over_the_day() -> None:
    def at(hour: int) -> int:
        return paced_allowance(datetime(2026, 11, 24, hour, tzinfo=TASHKENT), 200, 10)

    assert at(0) == 10  # only the burst right after midnight
    assert at(12) == 110  # half the cap + burst
    assert at(23) == 200  # never above the cap
