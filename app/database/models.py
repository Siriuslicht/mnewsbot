"""ORM models."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class ProcessedNews(Base):
    """A news item that has been handled (posted, skipped, or seen during initial sync).

    `hid` is unique, so the database itself guarantees an item is recorded
    once. Because the poller checks this table before posting, and the table
    lives on a persistent volume, restarts never re-post old news.
    """

    __tablename__ = "processed_news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # BigInteger so ids can never overflow on PostgreSQL. On SQLite it is the
    # same INTEGER type as before, so existing databases need no migration.
    hid: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class NewsScore(Base):
    """Claude's importance score for a news item, plus when it was posted.

    Stored so an item is never scored twice (e.g. when Telegram is down and the
    post is retried), and `posted_at` drives the daily post cap. A separate
    table, so existing databases need no migration: create_all adds it.
    """

    __tablename__ = "news_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hid: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    # NULL when the item was posted without scoring (IMPORTANCE_FILTER=false).
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class RecentPost(Base):
    """Text of posted headlines, so the repeat check survives restarts.

    A separate table, so existing databases need no migration. Old rows are
    pruned; only the latest few are ever read.
    """

    __tablename__ = "recent_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
