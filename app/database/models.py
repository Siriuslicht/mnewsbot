"""ORM models."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Integer
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
