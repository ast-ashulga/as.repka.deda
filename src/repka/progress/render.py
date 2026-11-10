"""Renderers (sinks) for stderr: `lines`, `jsonl` and `live`. `plain` is the legacy string callback, no sink.

Everything that came from outside (domain ids, details, notes) is cleaned with `textutil.printable()` and
collapsed to one line before it reaches a terminal or a JSON line.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO

from repka.progress.eta import JSONL_INTERVAL_S, LINES_INTERVAL_S, fmt_duration
from repka.textutil import printable

MODES = ("plain", "live", "lines", "jsonl")
_CLEAR = "\r\x1b[2K"


def _clean(text: object) -> str:
    return " ".join(printable(str(text)).split())


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _eta_text(overall: dict[str, Any]) -> str:
    seconds = overall.get("eta_seconds")
    if seconds is None:
        return "eta ?"
    at = overall.get("eta_at")
    clock = f" (at {datetime.fromtimestamp(at).strftime('%H:%M')})" if at else ""
    if overall.get("eta_is_lower_bound"):
        return f"eta >{fmt_duration(seconds)}{clock}"
    return f"eta ~{fmt_duration(seconds)}{clock}"


def _where(cur: dict[str, Any]) -> str:
    """`2/5 triage ai-agent (1/3) 400/1200 repos 33% readme 5/9`."""
    parts = [f"{cur['stage_index']}/{cur['stage_count']} {cur['stage']}"]
    step = cur.get("step")
    if step and step != cur["stage"]:
        parts.append(_clean(step))
    if cur.get("steps_total"):
        parts.append(f"({min(cur['steps_done'] + 1, cur['steps_total'])}/{cur['steps_total']})")
    total = cur.get("total")
    if total:
        unit = f" {_clean(cur['unit'])}" if cur.get("unit") else ""
        parts.append(f"{cur['done']}/{total}{unit} {100 * cur['done'] // total}%")
    if cur.get("detail"):
        parts.append(_clean(cur["detail"]))
    return " ".join(parts)


def status_line(snap: dict[str, Any]) -> str:
    cur = snap.get("current")
    head = _where(cur) if cur else f"run {snap['status']}"
    return f"{head} | {fmt_duration(snap['elapsed_s'])} | {_eta_text(snap['overall'])}"


class _Base:
    owns_stderr = False

    def __init__(self, stream: TextIO, clock: Callable[[], float], wall: Callable[[], float]) -> None:
        self._out, self._clock, self._wall = stream, clock, wall
        self._lock = threading.RLock()

    def _write(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()

    def _hms(self) -> str:
        return datetime.fromtimestamp(self._wall()).strftime("%H:%M:%S")

    def log_line(self, text: str, level: str = "INFO") -> None:
        with self._lock:
            self._write(printable(text).rstrip("\n") + "\n")


class LinesRenderer(_Base):
    """Timestamped lines for logs and agents; progress lines are throttled to one per LINES_INTERVAL_S."""

    def __init__(self, stream, clock, wall, interval: float = LINES_INTERVAL_S) -> None:
        super().__init__(stream, clock, wall)
        self._interval = interval
        self._last_progress = float("-inf")

    def on_event(self, event: str, snap: dict[str, Any], payload: dict[str, Any]) -> None:
        with self._lock:
            if event == "plan":
                self._line("plan " + " > ".join(payload["stages"]))
            elif event == "stage_start":
                self._line(status_line(snap) + " | started")
            elif event == "step":
                self._line(status_line(snap))
            elif event == "progress":
                now = self._clock()
                if now - self._last_progress >= self._interval:
                    self._last_progress = now
                    self._line(status_line(snap))
            elif event == "stage_end":
                st = next(s for s in snap["stages"] if s["name"] == payload["stage"])
                idx = [s["name"] for s in snap["stages"]].index(st["name"]) + 1
                detail = f" ({_clean(payload['detail'])})" if payload.get("detail") else ""
                self._line(f"{idx}/{len(snap['stages'])} {st['name']} {payload['status']} in {fmt_duration(st['seconds'])}{detail}")
            elif event == "note":
                self._line(_clean(payload["message"]))
            elif event == "finish":
                self._line(f"run {payload['status']} in {fmt_duration(snap['elapsed_s'])}")

    def _line(self, text: str) -> None:
        self._write(f"[{self._hms()}] {text}\n")


class JsonlRenderer(_Base):
    """One JSON object per stderr line, nothing else (logs and CLI messages included)."""

    owns_stderr = True

    def __init__(self, stream, clock, wall, interval: float = JSONL_INTERVAL_S) -> None:
        super().__init__(stream, clock, wall)
        self._interval = interval
        self._last_progress = float("-inf")

    def _ts(self) -> str:
        return datetime.fromtimestamp(self._wall(), UTC).isoformat(timespec="seconds")

    def _emit(self, obj: dict[str, Any]) -> None:
        self._write(json.dumps(_scrub(obj), ensure_ascii=False) + "\n")

    def on_event(self, event: str, snap: dict[str, Any], payload: dict[str, Any]) -> None:
        with self._lock:
            if event == "progress":
                now = self._clock()
                if now - self._last_progress < self._interval:
                    return
                self._last_progress = now
            obj: dict[str, Any] = {"ts": self._ts(), "event": "summary" if event == "finish" else event, **payload}
            if event == "note":
                self._emit(obj)
                return
            obj.update(status=snap["status"], elapsed_s=snap["elapsed_s"], current=snap["current"], overall=snap["overall"])
            if event == "finish":
                obj["stages"] = snap["stages"]
            self._emit(obj)

    def log_line(self, text: str, level: str = "INFO") -> None:
        with self._lock:
            self._emit({"ts": self._ts(), "event": "log", "level": level, "message": text})


class LiveRenderer(_Base):
    """One redrawn status line on a TTY; finished stages and log lines scroll above it."""

    owns_stderr = True
    REDRAW_S = 0.2
    BAR = 12

    def __init__(
        self, stream, clock, wall, columns: Callable[[], int] | None = None, unicode: bool | None = None, ticker: bool = False
    ) -> None:
        super().__init__(stream, clock, wall)
        self._want_ticker = ticker
        self._columns = columns or (lambda: shutil.get_terminal_size((100, 24)).columns)
        enc = (getattr(stream, "encoding", None) or "").lower()
        self._unicode = ("utf" in enc) if unicode is None else unicode
        self._snap: dict[str, Any] | None = None
        self._drawn = False
        self._last_draw = float("-inf")
        self._ticker: threading.Thread | None = None
        self._stop = threading.Event()

    # -- drawing -------------------------------------------------------------------------------

    def _bar(self, cur: dict[str, Any]) -> str:
        total = cur.get("total")
        if not total:
            return ""
        fill, empty = ("█", "░") if self._unicode else ("#", "-")
        n = min(self.BAR, self.BAR * cur["done"] // total)
        return f"[{fill * n}{empty * (self.BAR - n)}] "

    def _text(self) -> str:
        snap = self._snap
        if snap is None or snap["current"] is None:
            return ""
        cur = snap["current"]
        text = f"{'>' if not self._unicode else '▶'} {self._bar(cur)}{status_line(snap)}"
        return text[: max(10, self._columns() - 1)]

    def redraw(self) -> None:
        with self._lock:
            text = self._text()
            if text:
                self._write(_CLEAR + text)
                self._drawn = True
            else:
                self._erase()
            self._last_draw = self._clock()

    def _erase(self) -> None:
        if self._drawn:
            self._write(_CLEAR)
            self._drawn = False

    def _permanent(self, text: str) -> None:
        self._erase()
        self._write(text + "\n")

    def log_line(self, text: str, level: str = "INFO") -> None:
        with self._lock:
            self._permanent(printable(text).rstrip("\n"))
            if self._snap is not None:
                self.redraw()

    # -- events --------------------------------------------------------------------------------

    def on_event(self, event: str, snap: dict[str, Any], payload: dict[str, Any]) -> None:
        with self._lock:
            self._snap = snap
            if event == "stage_end":
                st = next(s for s in snap["stages"] if s["name"] == payload["stage"])
                mark = {"ok": "✓", "error": "✗", "skipped": "-"}[payload["status"]] if self._unicode else payload["status"]
                detail = f" ({_clean(payload['detail'])})" if payload.get("detail") else ""
                self._permanent(f"{mark} {st['name']} {fmt_duration(st['seconds'])}{detail}")
                self.redraw()
            elif event == "note":
                self.log_line(payload["message"])
            elif event == "finish":
                self.stop()
                self._snap = None
                self._erase()
                self._write(f"run {payload['status']} in {fmt_duration(snap['elapsed_s'])}\n")
            elif event in ("stage_start", "step") or self._clock() - self._last_draw >= self.REDRAW_S:
                self.redraw()
            if event == "stage_start" and self._want_ticker:
                self.start_ticker()

    # -- ticker: keeps elapsed moving between events ---------------------------------------------

    def start_ticker(self) -> None:
        if self._ticker is not None:
            return
        self._stop.clear()

        def run() -> None:
            while not self._stop.wait(1.0):
                try:
                    self.redraw()
                except Exception:
                    return

        self._ticker = threading.Thread(target=run, name="repka-live-ticker", daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        ticker, self._ticker = self._ticker, None
        if ticker is not None and ticker is not threading.current_thread():
            ticker.join(timeout=2)


def make_renderer(
    mode: str, stream: TextIO, *, is_tty: bool, clock: Callable[[], float], wall: Callable[[], float]
) -> LinesRenderer | JsonlRenderer | LiveRenderer | None:
    """`plain` -> None (the legacy string callback does the work); `live` without a TTY -> `lines`."""
    if mode == "plain":
        return None
    if mode == "jsonl":
        return JsonlRenderer(stream, clock, wall)
    if mode == "lines" or (mode == "live" and not is_tty):
        return LinesRenderer(stream, clock, wall)
    if mode == "live":
        return LiveRenderer(stream, clock, wall, ticker=True)
    raise ValueError(f"unknown progress mode {mode!r} (use one of {', '.join(MODES)})")
