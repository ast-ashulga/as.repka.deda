"""`.state/progress.json`: the run's snapshot, rewritten atomically while it holds the lock.

It is bookkeeping like the lock itself (written in every mode), so a launchd run on the default config is
observable too. The lock, not this file, tells whether a run is alive: a file that says `running` while nobody holds
`run.lock` belongs to a run that died.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from repka.progress.eta import HEARTBEAT_S

log = logging.getLogger("repka.progress")
SCHEMA = 1


def write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def liveness(state_dir: Path) -> Literal["running", "idle"]:
    """`running` when some process holds `run.lock`. The probe takes the lock and drops it at once."""
    path = Path(state_dir) / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "running"
        fcntl.flock(handle, fcntl.LOCK_UN)
        return "idle"


class StatusFileSink:
    """A progress sink that keeps the snapshot file current: always on stage/step/finish events, and as a
    heartbeat at most every HEARTBEAT_S on progress and note events."""

    def __init__(
        self,
        path: Path,
        pid: int,
        run_id: int,
        mode: str,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._path, self._pid, self._run_id, self._mode = Path(path), pid, run_id, mode
        self._clock, self._wall = clock, wall
        self._started_epoch = wall()
        self._last_write = float("-inf")

    def on_event(self, event: str, snap: dict[str, Any], payload: dict[str, Any]) -> None:
        now = self._clock()
        if event in ("progress", "note") and now - self._last_write < HEARTBEAT_S:
            return
        self._last_write = now
        data = {
            "schema": SCHEMA,
            "pid": self._pid,
            "run_id": self._run_id,
            "mode": self._mode,
            "started_epoch": self._started_epoch,
            "updated_epoch": self._wall(),
            **snap,
        }
        try:
            write_atomic(self._path, data)
        except OSError:
            log.debug("could not write %s", self._path, exc_info=True)
