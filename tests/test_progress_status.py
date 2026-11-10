import json
import os
import time

from repka import status_cli
from repka.cli import main
from repka.progress.events import RunProgress
from repka.progress.status import StatusFileSink, liveness, read_status
from repka.runlock import RunLock


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def make(tmp_path, clock=None, wall=None, history=None):
    clock = clock or Clock()
    wall = wall or Clock(1_700_000_000.0)
    path = tmp_path / "progress.json"
    sink = StatusFileSink(path, pid=4242, run_id=7, mode="lines", clock=clock, wall=wall)
    return RunProgress(clock=clock, wall=wall, sinks=[sink], history=history), path, clock, wall


def test_write_is_complete_and_atomic(tmp_path):
    p, path, clock, _ = make(tmp_path, history={"digest": 5.0})
    p.plan(["triage", "digest"])
    p.stage_start("triage", steps=1)
    p.step("dom", "repos", 100)
    clock.now += 10
    p.advance(40, 100)
    data = read_status(path)
    assert data["schema"] == 1 and data["pid"] == 4242 and data["run_id"] == 7 and data["mode"] == "lines"
    assert data["status"] == "running"
    assert data["current"]["stage"] == "triage" and data["current"]["done"] == 40
    assert data["overall"]["eta_seconds"] is not None
    assert "updated_epoch" in data and "started_epoch" in data
    assert not [f for f in os.listdir(tmp_path) if f != "progress.json"]  # no temp files left behind


def test_heartbeat_throttled_on_progress_but_not_on_stage_events(tmp_path):
    p, path, clock, wall = make(tmp_path)
    p.plan(["embed"])
    p.stage_start("embed", steps=1)
    p.step("d", "repos", 100)
    first = read_status(path)["updated_epoch"]
    wall.now += 1
    clock.now += 1
    p.advance(10, 100)  # within HEARTBEAT_S: not rewritten
    assert read_status(path)["updated_epoch"] == first
    wall.now += 10
    clock.now += 10
    p.advance(20, 100)  # heartbeat due
    assert read_status(path)["updated_epoch"] > first
    wall.now += 1
    p.stage_end("ok")  # stage events always write
    assert read_status(path)["stages"][0]["status"] == "ok"


def test_final_status_written_on_finish(tmp_path):
    p, path, clock, _ = make(tmp_path)
    p.plan(["digest"])
    p.stage_start("digest")
    p.stage_end("ok")
    p.finish("partial")
    data = read_status(path)
    assert data["status"] == "partial" and data["current"] is None


def test_read_status_missing_or_corrupt(tmp_path):
    assert read_status(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_status(bad) is None
    bad.write_text("[1, 2]")
    assert read_status(bad) is None


def test_sink_failure_does_not_raise(tmp_path):
    clock = Clock()
    sink = StatusFileSink(tmp_path / "missing-parent" / "x" / "progress.json", 1, 1, "plain", clock, Clock())
    os.chmod(tmp_path, 0o500)
    try:
        p = RunProgress(clock=clock, sinks=[sink])
        p.plan(["digest"])
        p.stage_start("digest")  # must not raise even if the directory cannot be created
    finally:
        os.chmod(tmp_path, 0o700)


def test_liveness(tmp_path):
    assert liveness(tmp_path) == "idle"
    with RunLock(tmp_path / "run.lock"):
        assert liveness(tmp_path) == "running"
    assert liveness(tmp_path) == "idle"  # released again, and the probe did not keep it


def write_file(home, **over):
    state = home / ".state"
    state.mkdir(exist_ok=True)
    now = time.time()
    data = {
        "schema": 1, "pid": os.getpid(), "run_id": 7, "mode": "lines", "status": "running",
        "started_at": "2026-11-10T10:00:00+00:00", "started_epoch": now - 80, "updated_epoch": now - 3,
        "elapsed_s": 77.0, "stages": [{"name": "triage", "status": "running", "seconds": 77.0, "items": 0, "detail": ""}],
        "current": {"stage": "triage", "stage_index": 1, "stage_count": 2, "steps_done": 0, "steps_total": 1,
                    "step": "ai-agent", "unit": "repos", "done": 400, "total": 1200, "detail": "", "rate": 5.0,
                    "eta_seconds": 160.0, "stage_elapsed_s": 77.0},
        "overall": {"eta_seconds": 170.0, "eta_is_lower_bound": False, "eta_at": now + 170},
    }  # fmt: skip
    data.update(over)
    (state / "progress.json").write_text(json.dumps(data))
    return state


def test_status_cli_running(home, capsys):
    state = write_file(home)
    with RunLock(state / "run.lock"):
        assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "running" in out and "run #7" in out and "triage" in out and "400/1200" in out
    assert "eta ~" in out and "heartbeat" in out
    assert "may be stuck" not in out


def test_status_cli_dead_run_is_not_reported_as_running(home, capsys):
    write_file(home)  # status says running, nobody holds the lock
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("not running") and "died" in out
    assert "eta" not in out


def test_status_cli_finished_run(home, capsys):
    write_file(home, status="partial", current=None)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("not running") and "partial" in out and "run #7" in out


def test_status_cli_json(home, capsys):
    state = write_file(home)
    with RunLock(state / "run.lock"):
        assert main(["status", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["state"] == "running" and obj["run"]["run_id"] == 7 and obj["stale"] is False
    assert 0 <= obj["heartbeat_age_s"] < 60


def test_status_cli_json_dead(home, capsys):
    write_file(home)
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "died"


def test_status_cli_no_file(home, capsys):
    assert main(["status"]) == 0
    assert "no run recorded yet" in capsys.readouterr().out
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "none"


def test_status_cli_warns_on_stale_heartbeat(home, capsys):
    state = write_file(home, updated_epoch=time.time() - 300)
    with RunLock(state / "run.lock"):
        assert main(["status"]) == 0
    assert "may be stuck" in capsys.readouterr().out


def test_status_module_is_registered():
    assert hasattr(status_cli, "register")


def test_status_cli_other_command_holding_the_lock_is_busy_not_a_stuck_run(home, capsys):
    state = write_file(home, status="ok", current=None, updated_epoch=time.time() - 5 * 3600)
    with RunLock(state / "run.lock"):  # e.g. `repka collect` is running: it never writes progress.json
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("busy") and "another repka command" in out and "may be stuck" not in out
        assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "busy"


def test_status_cli_lock_held_but_file_belongs_to_a_dead_pid_is_busy(home, capsys):
    state = write_file(home, pid=2_000_000_000)  # says running, but that process does not exist
    with RunLock(state / "run.lock"):
        assert main(["status"]) == 0
    assert capsys.readouterr().out.startswith("busy")


def test_stale_temp_files_are_removed_when_a_sink_starts(tmp_path):
    leftover = tmp_path / "progress.json.tmp999999"
    leftover.write_text("{")
    StatusFileSink(tmp_path / "progress.json", 1, 1, "plain", Clock(), Clock())
    assert not leftover.exists()


def test_heartbeat_thread_refreshes_the_file_without_events(tmp_path):
    import threading

    wall = Clock(1_700_000_000.0)
    path = tmp_path / "progress.json"
    sink = StatusFileSink(path, 1, 1, "plain", Clock(), wall, heartbeat_thread=True, heartbeat_s=0.02)
    p = RunProgress(clock=Clock(), wall=wall, sinks=[sink])
    p.plan(["triage"])
    p.stage_start("triage", steps=1)  # then a long silent worker call: no more events
    first = read_status(path)["updated_epoch"]
    wall.now += 60
    deadline = time.monotonic() + 3
    while read_status(path)["updated_epoch"] == first and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    assert read_status(path)["updated_epoch"] > first
    p.stage_end("ok")
    p.finish("ok")
    assert read_status(path)["status"] == "ok"
    final = read_status(path)["updated_epoch"]
    wall.now += 60
    threading.Event().wait(0.1)
    assert read_status(path)["updated_epoch"] == final  # the thread stopped with the run


def test_status_cli_lock_held_without_any_snapshot_is_busy(home, capsys):
    (home / ".state").mkdir()
    with RunLock(home / ".state" / "run.lock"):
        assert main(["status"]) == 0
        assert capsys.readouterr().out.startswith("busy: another repka process holds the run lock")
        assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "busy"
