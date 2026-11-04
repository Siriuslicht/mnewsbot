"""Single-instance guard for the scheduler.

If two copies of the bot ran against the same database (two uvicorn workers,
a manual `uvicorn` next to the container, a duplicated deploy), both would
see the same "new" items and post them twice. An OS file lock prevents that:
only the process holding the lock runs the scheduler. The lock is released
automatically by the OS if the process dies, so a crash never leaves it stuck.

This covers processes on one host sharing one volume, which is how this bot
is deployed. Running on several hosts would need a database-level lock instead.
"""

import fcntl
import os
from pathlib import Path
from typing import IO


class SingleInstanceLock:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._file: IO[str] | None = None

    def acquire(self) -> bool:
        """Try to take the lock without waiting. Returns False if another process holds it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
