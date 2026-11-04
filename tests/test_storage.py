from app.database.database import create_db_engine, create_session_factory, init_db, ping_db
from app.services.storage import NewsStorage
from app.utils.lock import SingleInstanceLock


def test_empty_then_filled(storage: NewsStorage) -> None:
    assert storage.is_empty()
    storage.mark_processed(1)
    assert not storage.is_empty()


def test_filter_new(storage: NewsStorage) -> None:
    storage.mark_processed(1)
    storage.mark_processed(2)
    assert storage.filter_new([1, 2, 3, 4]) == {3, 4}
    assert storage.filter_new([]) == set()


def test_mark_processed_is_idempotent(storage: NewsStorage) -> None:
    storage.mark_processed(7)
    storage.mark_processed(7)  # unique constraint hit, must not raise
    assert storage.filter_new([7]) == set()


def test_mark_many_skips_existing(storage: NewsStorage) -> None:
    storage.mark_processed(1)
    assert storage.mark_many([1, 2, 3]) == 2
    assert storage.mark_many([1, 2, 3]) == 0
    assert storage.filter_new([1, 2, 3]) == set()


def test_data_survives_restart(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Simulates a container restart: new engine, same file."""
    url = f"sqlite:///{tmp_path / 'nested' / 'db.db'}"
    first = create_db_engine(url)
    init_db(first)
    NewsStorage(create_session_factory(first)).mark_processed(42)
    first.dispose()

    second = create_db_engine(url)
    init_db(second)  # safe to run again
    assert NewsStorage(create_session_factory(second)).filter_new([42]) == set()
    second.dispose()


def test_ping_db(engine) -> None:  # type: ignore[no-untyped-def]
    assert ping_db(engine)


def test_ping_db_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    broken = create_db_engine(f"sqlite:///{tmp_path}")  # a directory, not a file
    assert not ping_db(broken)


def test_single_instance_lock(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "scheduler.lock")
    first, second = SingleInstanceLock(path), SingleInstanceLock(path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
