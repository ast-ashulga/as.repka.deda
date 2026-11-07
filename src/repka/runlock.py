"""Exclusive, non-blocking lock so two repka runs cannot write the same state."""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO


class LockBusy(RuntimeError):
    """Another process holds the lock."""


class RunLock:
    """Context manager around an flock(LOCK_EX | LOCK_NB). The OS releases it if the process dies."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._file: IO[str] | None = None

    def __enter__(self) -> RunLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise LockBusy(f"another repka run holds {self._path}") from None
        self._file = handle
        return self

    def __exit__(self, *exc: object) -> None:
        if self._file is not None:
            self._file.close()  # closing releases the lock
            self._file = None
