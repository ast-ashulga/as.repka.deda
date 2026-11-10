"""ETA arithmetic: throughput estimate, duration formatting and the history of earlier runs.

All tuning constants live here. An ETA is "at the current pace": rate-limit waits and worker model loads are
not predictable, so callers show it as an estimate and say so when only a lower bound is known.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Iterable

MIN_ELAPSED_S = 3.0  # no in-step ETA before this much time has passed
EWMA_ALPHA = 0.3  # weight of the newest throughput sample
LINES_INTERVAL_S = 10.0  # `lines` mode: at most one progress line per this many seconds
JSONL_INTERVAL_S = 1.0  # `jsonl` mode: at most one progress event per second
HEARTBEAT_S = 5.0  # status file: refresh at least this often while work advances
STALE_HEARTBEAT_S = 120.0  # `repka status`: warn when the heartbeat is older than this
HISTORY_RUNS = 5  # runs that did work in a stage, newest first, whose median predicts it


def fmt_duration(seconds: float) -> str:
    """45s, 2m10s, 1h05m."""
    s = int(round(max(0.0, seconds)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class RateEstimator:
    """Exponentially weighted throughput (items/s) over the intervals between `update` calls.

    A sample is the interval between two `update` calls where `done` actually grew; a call that reports no new
    progress (triage's per-README notifications) does not start a new interval, so time spent preparing a chunk
    counts. The first sample seeds the estimate; later ones blend in with EWMA_ALPHA, so a slowdown is followed
    within a few samples. Coarse progress (one update per chunk) is fine: samples are intervals, not items.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._start = clock()
        self._last_t = self._start
        self._last_done = 0
        self._rate: float | None = None

    def reset(self, total: int | None = None) -> None:
        self._start = self._last_t = self._clock()
        self._last_done = 0
        self._rate = None

    def update(self, done: int) -> None:
        now = self._clock()
        if done > self._last_done and now > self._last_t:
            sample = (done - self._last_done) / (now - self._last_t)
            self._rate = sample if self._rate is None else EWMA_ALPHA * sample + (1 - EWMA_ALPHA) * self._rate
        if done > self._last_done:
            self._last_done, self._last_t = done, now

    def elapsed(self) -> float:
        return self._clock() - self._start

    def rate(self) -> float | None:
        return self._rate

    def eta_seconds(self, done: int, total: int | None) -> float | None:
        """Seconds left at the current pace, or None while it is too early (or the total is unknown)."""
        if total is None:
            return None
        if done >= total:
            return 0.0
        if done <= 0 or self._rate is None or self._rate <= 0 or self.elapsed() < MIN_ELAPSED_S:
            return None
        return (total - done) / self._rate


def history_medians(rows: Iterable[str | None]) -> dict[str, float]:
    """Median seconds per stage from `runs.stats_json` strings (newest first).

    Only runs that did work in a stage count (`items > 0`; a not-due collect is not a sample), at most
    HISTORY_RUNS per stage. Rows without `timings`, with bad JSON or odd shapes are ignored.
    """
    samples: dict[str, list[float]] = {}
    for raw in rows:
        if not raw:
            continue
        try:
            timings = json.loads(raw).get("timings")
        except (ValueError, AttributeError):
            continue
        if not isinstance(timings, dict):
            continue
        for stage, entry in timings.items():
            try:
                seconds, items = float(entry["seconds"]), float(entry["items"])
            except (KeyError, TypeError, ValueError):
                continue
            bucket = samples.setdefault(str(stage), [])
            if items > 0 and len(bucket) < HISTORY_RUNS:
                bucket.append(seconds)
    return {stage: statistics.median(vals) for stage, vals in samples.items() if vals}
