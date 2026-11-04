"""Database engine, session factory and startup initialisation."""

import time
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.utils.logger import get_logger

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def create_db_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy engine for SQLite or PostgreSQL.

    - SQLite needs `check_same_thread=False` because FastAPI and the scheduler
      may use the connection from different threads. WAL mode lets the health
      check read while the poller writes, and is more crash-safe.
    - `pool_pre_ping` replaces dead connections (e.g. after a PostgreSQL
      restart) instead of failing the next query.
    """
    if database_url.startswith("sqlite"):
        _ensure_sqlite_dir(database_url)
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 15},
            pool_pre_ping=True,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        return engine

    return create_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def create_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine, attempts: int = 5, delay: float = 2.0) -> None:
    """Create tables if missing, retrying while the database comes up.

    `create_all` is idempotent and never alters existing tables, so it is safe
    on every start. We deliberately don't use a migration tool yet: there is
    one table and its schema has not changed. Add Alembic at the first schema
    change, because create_all would silently skip a new column on an existing
    database.
    """
    from app.database import models  # noqa: F401  # registers models on Base

    for attempt in range(1, attempts + 1):
        try:
            Base.metadata.create_all(engine)
            return
        except OperationalError as exc:
            if attempt == attempts:
                raise
            logger.warning("Database not ready (attempt %d/%d): %s", attempt, attempts, exc.orig)
            time.sleep(delay * attempt)


def ping_db(engine: Engine) -> bool:
    """Cheap availability check used by /health."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # any failure means "not available"
        logger.warning("Database health check failed", exc_info=True)
        return False


def _ensure_sqlite_dir(database_url: str) -> None:
    """Make sure the folder for the SQLite file exists (e.g. /app/data in Docker)."""
    path = database_url.split("///", 1)[-1]
    if path and path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
