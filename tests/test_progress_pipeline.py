import io
import json
import os

import pytest

from repka import pipeline, run_cli
from repka.cli import main
from repka.config import AppConfig, ConfigError, load_run
from repka.pipeline import Boundaries
from repka.progress.render import make_renderer
from repka.store import repo
from repka.store.db import connect, migrate
from repka.triage.deciders import WorkerError
from test_pipeline import Fakes, patch_boundaries, preflight_ok, write_domain  # noqa: F401  (autouse fixture)

FULL_CFG = "triage:\n  enabled: true\nembed:\n  enabled: true\n"


def stderr_lines(capsys):
    return [ln for ln in capsys.readouterr().err.splitlines() if ln]


def status_file(home):
    return json.loads((home / ".state" / "progress.json").read_text())


def test_load_run_progress_default_and_validation():
    assert load_run({}).progress == "plain"
    assert load_run({"progress": "jsonl"}).progress == "jsonl"
    with pytest.raises(ConfigError, match="run.progress"):
        load_run({"progress": "fast"})
    assert AppConfig().run.progress == "plain"


def test_bad_progress_in_config_is_exit_two(home, capsys):
    (home / ".config.yaml").write_text("run:\n  progress: fast\n")
    write_domain(home)
    assert main(["run"]) == 2
    assert "run.progress" in capsys.readouterr().err


def test_plain_mode_stderr_is_byte_identical(home, monkeypatch, capsys):
    write_domain(home)
    patch_boundaries(monkeypatch, Fakes())
    assert main(["run"]) == 0
    assert stderr_lines(capsys) == [
        "run #1: 1 domain(s), send=no",
        "collect: 1 enabled domain(s)",
        "digest: building a preview (not sent)",
    ]


def test_plain_mode_still_writes_the_status_file_and_finalises_it(home, monkeypatch, capsys):
    write_domain(home)
    patch_boundaries(monkeypatch, Fakes())
    assert main(["run"]) == 0
    data = status_file(home)
    assert data["status"] == "ok" and data["pid"] == os.getpid() and data["run_id"] == 1
    assert data["mode"] == "plain" and data["current"] is None
    assert [s["name"] for s in data["stages"]] == ["collect", "digest"]
    assert [s["status"] for s in data["stages"]] == ["skipped", "ok"]  # the fake collect found nothing due


def test_lines_mode_reports_every_stage_in_order(home, monkeypatch, capsys):
    write_domain(home)
    (home / ".config.yaml").write_text(FULL_CFG)
    patch_boundaries(monkeypatch, Fakes())
    assert main(["run", "--progress", "lines"]) == 0
    lines = stderr_lines(capsys)
    ended = [ln for ln in lines if " ok in " in ln or " skipped in " in ln]
    assert [ln.split("] ", 1)[1].split()[1] for ln in ended[:4]] == ["collect", "triage", "embed", "digest"]
    assert lines[-1].split("] ", 1)[1].startswith("run ok in")
    assert any("plan collect > triage > embed > digest" in ln for ln in lines)
    assert any("triage: alpha: decided 0" in ln for ln in lines)  # outcome notes reach the log in non-plain modes


def test_config_selects_the_mode_and_the_flag_overrides_it(home, monkeypatch, capsys):
    write_domain(home)
    (home / ".config.yaml").write_text("run:\n  progress: lines\n")
    patch_boundaries(monkeypatch, Fakes())
    assert main(["run"]) == 0
    timestamped = [ln for ln in stderr_lines(capsys) if ln.startswith("[")]
    assert timestamped and any("collect" in ln for ln in timestamped)
    assert main(["run", "--progress", "plain"]) == 0  # the flag wins over run.progress
    assert not [ln for ln in stderr_lines(capsys) if ln.startswith("[")]


def test_jsonl_mode_every_stderr_line_is_json_and_stdout_untouched(home, monkeypatch, capsys):
    write_domain(home)
    (home / ".config.yaml").write_text(FULL_CFG)
    patch_boundaries(monkeypatch, Fakes(triage=WorkerError("worker timed out")))
    assert main(["run", "--json", "--progress", "jsonl"]) == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)  # stdout is still exactly the result JSON
    assert out["status"] == "partial" and "timings" not in out
    objs = [json.loads(ln) for ln in captured.err.splitlines() if ln]  # every stderr line parses
    events = [o["event"] for o in objs]
    assert events[0] == "log" and objs[0]["message"].startswith("run #1:")
    assert events[-2:] == ["summary", "log"]  # the final `run #N: ...` line is a log event too
    ends = {o["stage"]: o["status"] for o in objs if o["event"] == "stage_end"}
    assert ends["triage"] == "error" and ends["collect"] == "skipped" and ends["digest"] == "ok"
    plan = next(o for o in objs if o["event"] == "plan")
    assert plan["stages"] == ["collect", "triage", "embed", "digest"]
    assert any(o["event"] == "log" and o["message"].startswith("run #1: partial") for o in objs)


def test_disabled_stages_are_left_out_of_the_plan(home, monkeypatch, capsys):
    write_domain(home)
    patch_boundaries(monkeypatch, Fakes())
    assert main(["run", "--progress", "jsonl"]) == 0
    plan = next(json.loads(ln) for ln in stderr_lines(capsys) if json.loads(ln)["event"] == "plan")
    assert plan["stages"] == ["collect", "digest"]  # triage and embed are off by default


def test_runs_row_has_timings_and_the_old_shape_intact(home, monkeypatch, capsys):
    write_domain(home)
    patch_boundaries(monkeypatch, Fakes())
    assert main(["run"]) == 0
    conn = connect(home / ".state" / "repka.db")
    row = conn.execute("SELECT stats_json FROM runs").fetchone()
    conn.close()
    stats = json.loads(row["stats_json"])
    assert stats["digest"]["status"] == "preview" and "parts" not in stats["digest"] and "errors" in stats
    assert stats["timings"]["digest"]["items"] == 1 and "collect" not in stats["timings"]  # not-due collect: no sample


def seed_history(home, rows):
    conn = connect(home / ".state" / "repka.db")
    migrate(conn)
    for timings in rows:
        run_id = repo.start_run(conn, "run")
        repo.finish_run(conn, run_id, "ok", {"timings": timings} if timings is not None else {}, None)
    conn.commit()
    conn.close()


def test_history_feeds_the_overall_eta(home, monkeypatch, capsys):
    write_domain(home)
    seed_history(home, [None, {"collect": {"seconds": 90.0, "items": 4}, "digest": {"seconds": 3.0, "items": 1}}])
    patch_boundaries(monkeypatch, Fakes())
    assert main(["run", "--progress", "lines"]) == 0
    started = [ln for ln in stderr_lines(capsys) if "1/2 collect" in ln and "started" in ln]
    assert started and "eta ~" in started[0]  # old rows without timings were ignored, the real one used


def test_stage_without_history_gives_an_unknown_eta_not_a_made_up_one(home, monkeypatch, capsys):
    write_domain(home)
    patch_boundaries(monkeypatch, Fakes())
    assert main(["run", "--progress", "lines"]) == 0
    started = [ln for ln in stderr_lines(capsys) if "1/2 collect" in ln and "started" in ln]
    assert started and "eta ?" in started[0]


def crashing(monkeypatch, exc):
    def build(conn, cfg, domains, **kwargs):
        def collect(ds, now):
            raise exc

        fakes = Fakes().boundaries()
        return Boundaries(collect, fakes.triage, fakes.embed, fakes.digest, releases=fakes.releases)

    monkeypatch.setattr(pipeline, "build_boundaries", build)


def test_ctrl_c_finalises_status_file_and_ends_the_line(home, monkeypatch, capsys):
    write_domain(home)
    crashing(monkeypatch, KeyboardInterrupt())
    buf = io.StringIO()
    real = make_renderer
    monkeypatch.setattr(run_cli, "make_renderer", lambda mode, stream, **kw: real(mode, buf, **{**kw, "is_tty": True}))
    with pytest.raises(KeyboardInterrupt):
        main(["run", "--progress", "live"])
    assert status_file(home)["status"] == "failed"
    assert buf.getvalue().endswith("\n") and "run failed" in buf.getvalue().splitlines()[-1]
    conn = connect(home / ".state" / "repka.db")
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "failed"
    conn.close()


def test_crash_in_jsonl_mode_still_prints_json_only(home, monkeypatch, capsys):
    write_domain(home)
    crashing(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        main(["run", "--progress", "jsonl"])
    for ln in stderr_lines(capsys):
        json.loads(ln)
    assert status_file(home)["status"] == "failed"
