"""Exclusive, non-blocking lock so two repka runs cannot write the same state."""

from __future__ import annotations

import fcntl
import sys
from collections.abc import Callable
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


def run_exclusive(fn: Callable[[], int], *, enabled: bool = True) -> int:
    """Run a state-writing command under the shared run lock; a busy lock is a friendly exit 0 (like `collect`).
    `enabled=False` (a read-only mode, e.g. --dry-run) runs `fn` without the lock."""
    if not enabled:
        return fn()
    from repka.config import state_dir

    try:
        with RunLock(state_dir() / "run.lock"):
            return fn()
    except LockBusy as exc:
        print(f"another repka run is in progress ({exc}); nothing done", file=sys.stderr)
        return 0
