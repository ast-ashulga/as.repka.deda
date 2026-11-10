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
import threading
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
    """A progress sink that keeps the snapshot file current: always on stage/step/finish events, as a heartbeat
    at most every HEARTBEAT_S on progress and note events, and (with `heartbeat_thread`) by a daemon thread that
    refreshes `updated_epoch` (heartbeat) while no event arrives, e.g. during one long worker call. `progress_epoch`
    moves only with real events, so `repka status` can tell a quiet run (alive, no event for a while) from a frozen
    process (no heartbeat), and shows how long it has been since work last advanced."""

    def __init__(
        self,
        path: Path,
        pid: int,
        run_id: int,
        mode: str,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        heartbeat_thread: bool = False,
        heartbeat_s: float = HEARTBEAT_S,
    ) -> None:
        self._path, self._pid, self._run_id, self._mode = Path(path), pid, run_id, mode
        self._clock, self._wall = clock, wall
        self._started_epoch = wall()
        self._last_write = float("-inf")
        self._data: dict[str, Any] | None = None
        self._progress_epoch = wall()  # last real event; the heartbeat thread never touches it
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._want_thread, self._interval = heartbeat_thread, heartbeat_s
        self._thread: threading.Thread | None = None
        for stale in self._path.parent.glob(self._path.name + ".tmp*"):  # left by a process killed mid-write
            try:
                stale.unlink()
            except OSError:
                pass

    def on_event(self, event: str, snap: dict[str, Any], payload: dict[str, Any]) -> None:
        self._progress_epoch = self._wall()
        now = self._clock()
        if event in ("progress", "note") and now - self._last_write < HEARTBEAT_S:
            return
        self._last_write = now
        with self._lock:
            self._data = {
                "schema": SCHEMA,
                "pid": self._pid,
                "run_id": self._run_id,
                "mode": self._mode,
                "started_epoch": self._started_epoch,
                "updated_epoch": self._wall(),  # heartbeat: the process is alive
                "progress_epoch": self._progress_epoch,  # last real event: work is advancing
                **snap,
            }
            self._write()
        if event == "finish":
            self._stop.set()
        elif self._want_thread and self._thread is None:
            self._thread = threading.Thread(target=self._beat, name="repka-status-heartbeat", daemon=True)
            self._thread.start()

    def _write(self) -> None:
        try:
            write_atomic(self._path, self._data or {})
        except OSError:
            log.debug("could not write %s", self._path, exc_info=True)

    def _beat(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                if self._stop.is_set() or self._data is None:
                    return
                self._data["updated_epoch"] = self._wall()
                self._data["progress_epoch"] = self._progress_epoch  # events that were throttled out of a write
                self._write()
