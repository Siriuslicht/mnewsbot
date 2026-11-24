"""Duplicate-prevention storage backed by the processed_news table."""

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database.models import NewsScore, ProcessedNews
from app.services.importance import ImportanceScore
from app.utils.logger import get_logger

logger = get_logger(__name__)


class NewsStorage:
    """Remembers which news ids (hid) have been handled.

    These calls are synchronous: queries on this small, indexed table take
    well under a millisecond, so moving them to threads would add complexity
    for no gain. Callers catch SQLAlchemyError; this class doesn't hide failures.
    """

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def is_empty(self) -> bool:
        """True when nothing has ever been recorded (first run)."""
        with self._session_factory() as session:
            return not session.scalar(select(exists().where(ProcessedNews.id.is_not(None))))

    def filter_new(self, hids: Iterable[int]) -> set[int]:
        """Return only the hids that have NOT been processed yet (one query)."""
        hids = set(hids)
        if not hids:
            return set()
        with self._session_factory() as session:
            existing = session.scalars(
                select(ProcessedNews.hid).where(ProcessedNews.hid.in_(hids))
            ).all()
        return hids - set(existing)

    def mark_processed(self, hid: int) -> None:
        """Record one hid after it was posted (or deliberately skipped)."""
        with self._session_factory() as session:
            session.add(ProcessedNews(hid=hid))
            try:
                session.commit()
            except IntegrityError:
                # Already stored. Harmless: the unique constraint did its job.
                session.rollback()
                logger.debug("hid=%s already marked as processed", hid)

    # ---- importance scores and the daily cap ------------------------------

    def get_scores(self, hids: Iterable[int]) -> dict[int, ImportanceScore]:
        """Scores already stored for these hids (items scored in an earlier cycle)."""
        hids = set(hids)
        if not hids:
            return {}
        with self._session_factory() as session:
            rows = session.scalars(
                select(NewsScore).where(NewsScore.hid.in_(hids), NewsScore.score.is_not(None))
            ).all()
        return {row.hid: ImportanceScore(row.score, row.category or "other") for row in rows}

    def save_scores(self, scores: dict[int, ImportanceScore]) -> None:
        if not scores:
            return
        with self._session_factory() as session:
            existing = set(
                session.scalars(select(NewsScore.hid).where(NewsScore.hid.in_(scores))).all()
            )
            session.add_all(
                NewsScore(hid=hid, score=s.score, category=s.category)
                for hid, s in sorted(scores.items())
                if hid not in existing
            )
            session.commit()

    def mark_posted(self, hid: int) -> None:
        """Record the post time; this is what the daily cap counts."""
        now = datetime.now(UTC)
        with self._session_factory() as session:
            row = session.scalar(select(NewsScore).where(NewsScore.hid == hid))
            if row is None:
                session.add(NewsScore(hid=hid, posted_at=now))
            else:
                row.posted_at = now
            session.commit()

    def count_posted_since(self, since: datetime) -> int:
        with self._session_factory() as session:
            return session.scalar(
                select(func.count()).select_from(NewsScore).where(NewsScore.posted_at >= since)
            ) or 0

    def mark_many(self, hids: Iterable[int]) -> int:
        """Record many hids in one transaction (initial sync). Returns how many were new."""
        new_hids = self.filter_new(hids)
        if not new_hids:
            return 0
        with self._session_factory() as session:
            session.add_all(ProcessedNews(hid=hid) for hid in sorted(new_hids))
            session.commit()
        return len(new_hids)
