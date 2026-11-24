"""Shared fixtures. No test touches the real network, Telegram, or your .env."""

import os

# Must be set before any `app` import: Settings are read at import time by app.main.
# setdefault-free on purpose: tests must never pick up real credentials.
os.environ.update(
    BOT_TOKEN="123456789:TEST_token_abcdefghijklmnopqrstuvwxyz",
    CHANNEL_ID="@test_channel",
    ENVIRONMENT="development",
    PTB_TIMEDELTA="true",
    IMPORTANCE_FILTER="false",
    TRANSLATE_UZ="false",
)

from collections.abc import Callable  # noqa: E402

import pytest  # noqa: E402

from app.database.database import create_db_engine, create_session_factory, init_db  # noqa: E402
from app.services.marketnews import NewsItem  # noqa: E402
from app.services.storage import NewsStorage  # noqa: E402


@pytest.fixture
def engine(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def storage(engine) -> NewsStorage:  # type: ignore[no-untyped-def]
    return NewsStorage(create_session_factory(engine))


@pytest.fixture
def make_item() -> Callable[..., NewsItem]:
    def _make(hid: int, **overrides: object) -> NewsItem:
        data: dict[str, object] = {
            "hid": hid,
            "ts": 1_700_000_000 + hid,
            "text": f"News {hid}",
            "link": f"https://example.com/{hid}",
            "cat": "STOCKS",
            "dir": "bull",
        }
        data.update(overrides)
        return NewsItem.model_validate(data)

    return _make
