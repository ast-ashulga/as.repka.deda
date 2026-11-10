"""Progress events: the `Reporter` protocol and `RunProgress`, the state machine behind every renderer.

A *stage* is collect / triage / embed / digest / releases; a *step* is one unit inside it (a domain, or collect's
"search" and "enrich" phases). `RunProgress` keeps the numbers, computes ETAs and fans every event out to
*sinks* (renderers, the status file). A sink that raises is ignored: observability never fails a stage.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from repka.progress.eta import RateEstimator

log = logging.getLogger("repka.progress")

StageStatus = Literal["ok", "error", "skipped"]


class Reporter(Protocol):
    def plan(self, stages: list[str]) -> None: ...
    def stage_start(self, stage: str, steps: int | None = None) -> None: ...
    def step(self, label: str, unit: str = "", total: int | None = None) -> None: ...
    def advance(self, done: int, total: int | None = None, detail: str = "") -> None: ...
    def stage_end(self, status: StageStatus, detail: str = "", items: int | None = None) -> None: ...
    def note(self, message: str) -> None: ...
    def finish(self, status: str) -> None: ...


class Sink(Protocol):
    def on_event(self, event: str, snapshot: dict[str, Any], payload: dict[str, Any]) -> None: ...


def notify(callback: Callable[..., None] | None, *args: Any) -> None:
    """Call an optional progress callback; whatever it does, a stage never fails because of it."""
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        log.debug("progress callback failed", exc_info=True)


class NullReporter:
    """Accepts every call and does nothing (tests, library use)."""

    def plan(self, stages: list[str]) -> None:
        pass

    def stage_start(self, stage: str, steps: int | None = None) -> None:
        pass

    def step(self, label: str, unit: str = "", total: int | None = None) -> None:
        pass

    def advance(self, done: int, total: int | None = None, detail: str = "") -> None:
        pass

    def stage_end(self, status: StageStatus, detail: str = "", items: int | None = None) -> None:
        pass

    def note(self, message: str) -> None:
        pass

    def finish(self, status: str) -> None:
        pass


@dataclass
class _Step:
    label: str
    unit: str
    total: int | None
    started: float
    estimator: RateEstimator
    done: int = 0
    detail: str = ""


@dataclass
class _Stage:
    name: str
    status: str = "pending"  # pending | running | ok | error | skipped
    started: float | None = None
    seconds: float = 0.0
    steps_total: int | None = None
    step_seconds: list[float] = field(default_factory=list)
    items: int = 0
    detail: str = ""


class RunProgress:
    """Implements `Reporter`; `snapshot()` is the one shape all renderers and the status file consume."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        history: dict[str, float] | None = None,
        sinks: Sequence[Sink] = (),
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._history = history or {}
        self._sinks = list(sinks)
        self._stages: dict[str, _Stage] = {}
        self._current: _Stage | None = None
        self._step: _Step | None = None
        self._started = clock()
        self._started_wall = wall()
        self._status = "running"

    # ---- Reporter ---------------------------------------------------------------------------

    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    def plan(self, stages: list[str]) -> None:
        for name in stages:
            self._stages.setdefault(name, _Stage(name))
        self._emit("plan", {"stages": list(stages)})

    def stage_start(self, stage: str, steps: int | None = None) -> None:
        st = self._stages.setdefault(stage, _Stage(stage))
        st.status, st.started, st.steps_total = "running", self._clock(), steps
        self._current, self._step = st, None
        self._emit("stage_start", {"stage": stage, "steps": steps})

    def step(self, label: str, unit: str = "", total: int | None = None) -> None:
        self._close_step()
        self._step = _Step(label, unit, total, self._clock(), RateEstimator(self._clock))
        self._emit("step", {"label": label, "unit": unit, "total": total})

    def advance(self, done: int, total: int | None = None, detail: str = "") -> None:
        if self._current is None:
            return
        if self._step is None:
            self.step(self._current.name, "", total)
        s = self._step
        assert s is not None
        if total is not None:
            s.total = total
        s.done, s.detail = done, detail
        s.estimator.update(done)
        self._emit("progress", {"done": done, "total": s.total, "detail": detail})

    def stage_end(self, status: StageStatus, detail: str = "", items: int | None = None) -> None:
        st = self._current
        if st is None:
            return
        self._close_step()
        st.status, st.detail = status, detail
        st.seconds = self._clock() - (st.started if st.started is not None else self._clock())
        if items is not None:
            st.items = items
        self._current = None
        self._emit("stage_end", {"stage": st.name, "status": status, "detail": detail})

    def note(self, message: str) -> None:
        self._emit("note", {"message": message})

    def finish(self, status: str) -> None:
        if self._status != "running":
            return
        if self._current is not None:  # a stage still open means the run was cut short
            self.stage_end("error", "interrupted")
        self._status = status
        self._emit("finish", {"status": status})

    # ---- results ----------------------------------------------------------------------------

    def timings(self) -> dict[str, dict[str, float]]:
        """`{stage: {"seconds", "items"}}` for stages that ran: the history later ETAs are built from."""
        return {
            s.name: {"seconds": round(s.seconds, 1), "items": s.items}
            for s in self._stages.values()
            if s.status in ("ok", "error")
        }

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        names = list(self._stages)
        stages = [
            {
                "name": s.name,
                "status": s.status,
                "seconds": round(s.seconds if s.status != "running" else now - (s.started or now), 1),
                "items": s.items,
                "detail": s.detail,
            }
            for s in self._stages.values()
        ]
        current = self._current_view(now, names)
        return {
            "status": self._status,
            "started_at": datetime.fromtimestamp(self._started_wall, UTC).isoformat(timespec="seconds"),
            "elapsed_s": round(now - self._started, 1),
            "stages": stages,
            "current": current,
            "overall": self._overall(now),
        }

    # ---- internals --------------------------------------------------------------------------

    def _close_step(self) -> None:
        if self._step is not None and self._current is not None:
            self._current.step_seconds.append(self._clock() - self._step.started)
            self._current.items += self._step.done
        self._step = None

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if not self._sinks:
            return
        try:
            snap = self.snapshot()
        except Exception:  # never let bookkeeping break a stage
            log.debug("progress snapshot failed", exc_info=True)
            return
        for sink in self._sinks:
            try:
                sink.on_event(event, snap, payload)
            except Exception:
                log.debug("progress sink %r failed on %s", sink, event, exc_info=True)

    def _stage_remaining(self, now: float) -> tuple[float, bool]:
        """(seconds known to remain in the running stage, whether that is the whole story)."""
        st, step = self._current, self._step
        if st is None:
            return 0.0, True
        in_step = step.estimator.eta_seconds(step.done, step.total) if step else None
        mean = sum(st.step_seconds) / len(st.step_seconds) if st.step_seconds else None
        if st.steps_total is not None:
            left_after = max(0, st.steps_total - len(st.step_seconds) - (1 if step else 0))
            if step is None:  # between two steps
                if mean is not None:
                    return left_after * mean, True
            elif in_step is not None:
                if left_after == 0:
                    return in_step, True
                if mean is not None:
                    return in_step + left_after * mean, True
        hist = self._history.get(st.name)  # a stage of unknown shape (or too early): fall back to history
        if hist is not None:
            return max(hist - (now - (st.started or now)), in_step or 0.0), True
        return in_step or 0.0, False

    def _overall(self, now: float) -> dict[str, Any]:
        if self._status != "running":
            return {"eta_seconds": 0.0, "eta_is_lower_bound": False, "eta_at": None}
        total, exact = self._stage_remaining(now)
        for s in self._stages.values():
            if s.status == "pending":
                hist = self._history.get(s.name)
                if hist is None:
                    exact = False
                else:
                    total += hist
        if total <= 0 and not exact:
            return {"eta_seconds": None, "eta_is_lower_bound": False, "eta_at": None}
        return {
            "eta_seconds": round(total, 1),
            "eta_is_lower_bound": not exact,
            "eta_at": self._wall() + total,
        }

    def _current_view(self, now: float, names: list[str]) -> dict[str, Any] | None:
        st, step = self._current, self._step
        if st is None:
            return None
        stage_eta, _ = self._stage_remaining(now)
        view: dict[str, Any] = {
            "stage": st.name,
            "stage_index": names.index(st.name) + 1,
            "stage_count": len(names),
            "steps_done": len(st.step_seconds),
            "steps_total": st.steps_total,
            "stage_elapsed_s": round(now - (st.started or now), 1),
            "step": step.label if step else None,
            "unit": step.unit if step else "",
            "done": step.done if step else 0,
            "total": step.total if step else None,
            "detail": step.detail if step else "",
            "rate": step.estimator.rate() if step else None,
            "eta_seconds": None,
        }
        if step is not None:
            in_step = step.estimator.eta_seconds(step.done, step.total)
            view["eta_seconds"] = round(stage_eta, 1) if in_step is not None else None
        return view
