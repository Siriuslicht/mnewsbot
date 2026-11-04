"""Duplicate-prevention storage backed by the processed_news table."""

from collections.abc import Iterable

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database.models import ProcessedNews
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

    def mark_many(self, hids: Iterable[int]) -> int:
        """Record many hids in one transaction (initial sync). Returns how many were new."""
        new_hids = self.filter_new(hids)
        if not new_hids:
            return 0
        with self._session_factory() as session:
            session.add_all(ProcessedNews(hid=hid) for hid in sorted(new_hids))
            session.commit()
        return len(new_hids)
