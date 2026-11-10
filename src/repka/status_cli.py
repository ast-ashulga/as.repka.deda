"""`repka status [--json]`: what a running `repka run` is doing now, or how the last one ended."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from typing import Any

from repka.config import state_dir
from repka.progress.eta import STALE_HEARTBEAT_S, fmt_duration
from repka.progress.render import status_line
from repka.progress.status import liveness, read_status

EPILOG = """\
The lock decides: a snapshot that says `running` while no process holds `.state/run.lock` is a run that died.
The snapshot (`.state/progress.json`) is written by every `repka run`, whatever `run.progress` is. Exit 0 whenever
the state can be reported. `--json` prints {state: running|busy|idle|died|none, stale, heartbeat_age_s, run}.
`busy` means the lock is held by something that is not the run in the snapshot (e.g. `repka collect`).
"""


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError, OverflowError):
        return isinstance(pid, int)  # exists but is not ours; a junk value is not a process
    return True


def inspect(now: float | None = None) -> dict[str, Any]:
    """State of the last or current run: {state, stale, heartbeat_age_s, run}."""
    now = time.time() if now is None else now
    run = read_status(state_dir() / "progress.json")
    if run is None:
        state = "busy" if liveness(state_dir()) == "running" else "none"
        return {"state": state, "stale": False, "heartbeat_age_s": None, "run": None}
    age = max(0.0, now - float(run.get("updated_epoch", now)))
    alive = liveness(state_dir()) == "running"
    if alive and run.get("status") == "running" and _pid_alive(run.get("pid")):
        state = "running"
    elif alive:  # another command (collect, triage, digest --send, ...) holds the lock; it writes no snapshot
        state = "busy"
    elif run.get("status") == "running":
        state = "died"
    else:
        state = "idle"
    return {"state": state, "stale": state == "running" and age > STALE_HEARTBEAT_S, "heartbeat_age_s": round(age, 1), "run": run}


def _ago(seconds: float | None) -> str:
    return "?" if seconds is None else f"{fmt_duration(seconds)} ago"


def render(info: dict[str, Any], now: float | None = None) -> str:
    now = time.time() if now is None else now
    run, state, age = info["run"], info["state"], info["heartbeat_age_s"]
    if state == "none":
        return "no run recorded yet (nothing has run in this repka home)"
    if state == "busy" and run is None:
        return "busy: another repka process holds the run lock, and no progress snapshot exists (yet)"
    if state == "busy":
        return (
            "busy: another repka command holds the run lock (collect, triage, digest --send, ...); "
            f"it reports no progress here. The last `repka run` was #{run['run_id']}, {run.get('status')}, "
            f"updated {_ago(age)}"
        )
    head = f"run #{run['run_id']} (pid {run['pid']}), progress mode {run.get('mode', '?')}"
    if state == "running":
        snap = {**run, "elapsed_s": now - float(run.get("started_epoch", now))}
        lines = [f"running: {head}", f"  {status_line(snap)}", f"  heartbeat {_ago(age)}"]
        if info["stale"]:
            lines.append(f"  warning: no heartbeat for {fmt_duration(age or 0)} (limit {fmt_duration(STALE_HEARTBEAT_S)}): it may be stuck")
        return "\n".join(lines)
    stage = (run.get("current") or {}).get("stage") or next(
        (s["name"] for s in run.get("stages", []) if s["status"] == "running"), None
    )
    if state == "died":
        where = f" in stage {stage}" if stage else ""
        return f"not running: {head} died without finishing{where}; last heartbeat {_ago(age)}"
    took = fmt_duration(run.get("elapsed_s", 0))
    when = datetime.fromtimestamp(run.get("updated_epoch", now)).astimezone().strftime("%Y-%m-%d %H:%M")
    return f"not running: last {head} ended {run.get('status')} at {when} ({_ago(age)}), took {took}"


def _status(args: argparse.Namespace) -> int:
    info = inspect()
    print(json.dumps(info, ensure_ascii=False, indent=2) if args.json else render(info))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "status",
        help="what a running `repka run` is doing now, or how the last one ended",
        description="Show the current stage, progress, elapsed time and ETA of a running pass, or how the last one ended.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="print the state as JSON on stdout")
    p.set_defaults(func=_status)
