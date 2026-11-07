import json
from datetime import UTC, datetime

import pytest

from repka import pipeline, preflight
from repka.cli import main
from repka.collect import CollectReport, DomainReport
from repka.config import AppConfig, EmbedConfig, TriageConfig
from repka.digest.run import DigestOutcome
from repka.domain.schema import Domain, SearchSpec
from repka.embed.embed import EmbedStats
from repka.embed.ollama import EmbedderUnavailable
from repka.github.client import RateLimited
from repka.pipeline import Boundaries, explain, run_pass
from repka.runlock import RunLock
from repka.store import repo
from repka.store.db import connect, migrate
from repka.triage.deciders import WorkerError
from repka.triage.triage import TriageStats

NOW = datetime(2026, 11, 8, 8, 0, 0, tzinfo=UTC)
ON = AppConfig(triage=TriageConfig(enabled=True), embed=EmbedConfig(enabled=True))
OFF = AppConfig()


def domain(domain_id="alpha", enabled=True) -> Domain:
    return Domain(id=domain_id, name=domain_id, enabled=enabled, profile="p",
                  search=SearchSpec(keywords=(), topics=("mcp",), languages=(), exclude=()))


class Fakes:
    """Fake boundaries that record the order of calls; each stage can be told to raise."""

    def __init__(self, collect=None, triage=None, embed=None, digest=None):
        self.calls: list[tuple] = []
        self.overrides = {"collect": collect, "triage": triage, "embed": embed, "digest": digest}

    def _stage(self, name, *args, default):
        self.calls.append((name, *args))
        value = self.overrides[name]
        if isinstance(value, Exception):
            raise value
        return default if value is None else value

    def boundaries(self) -> Boundaries:
        return Boundaries(
            collect=lambda ds, now: self._stage("collect", [d.id for d in ds], default=CollectReport()),
            triage=lambda d, now: self._stage("triage", d.id, default=TriageStats()),
            embed=lambda d, now: self._stage("embed", d.id, default=EmbedStats()),
            digest=lambda now, send: self._stage("digest", send, default=DigestOutcome("preview", "08:30", "2026-11-08", 1, ["x"])),
        )

    @property
    def names(self):
        return [c[0] for c in self.calls]


def test_stage_order_and_flags_off_skip_triage_and_embed():
    fakes = Fakes()
    report = run_pass([domain("a"), domain("b"), domain("off", enabled=False)], ON, fakes.boundaries(), NOW, send=False)
    assert fakes.names == ["collect", "triage", "triage", "embed", "embed", "digest"]
    assert fakes.calls[0] == ("collect", ["a", "b"])  # disabled domains are never collected
    assert report.status == "ok" and list(report.triage) == ["a", "b"]

    fakes = Fakes()
    report = run_pass([domain()], OFF, fakes.boundaries(), NOW, send=False)
    assert fakes.names == ["collect", "digest"] and report.status == "ok"  # digest is always evaluated


@pytest.mark.parametrize(
    "collect",
    [
        RateLimited("slow down", None),  # raised ...
        CollectReport([DomainReport("alpha", "partial", error="GitHub rate limit")], rate_limited=True),  # ... or reported
    ],
    ids=["raised", "reported"],
)
def test_rate_limited_run_is_partial_and_continues_to_digest(collect):  # RF1
    fakes = Fakes(collect=collect)
    report = run_pass([domain()], ON, fakes.boundaries(), NOW, send=True)
    assert report.status == "partial"
    assert fakes.names == ["collect", "triage", "embed", "digest"] and ("digest", True) in fakes.calls
    assert report.errors


def test_worker_error_is_partial_and_continues():  # RF7
    fakes = Fakes(triage=WorkerError("worker timed out"))
    report = run_pass([domain("a"), domain("b")], ON, fakes.boundaries(), NOW, send=True)
    assert report.status == "partial"
    assert fakes.names == ["collect", "triage", "embed", "embed", "digest"]  # stage stops at the first WorkerError
    assert "worker timed out" in report.errors[0]


def test_embedder_unavailable_is_partial():
    fakes = Fakes(embed=EmbedderUnavailable("Ollama is not running"))
    report = run_pass([domain()], ON, fakes.boundaries(), NOW, send=False)
    assert report.status == "partial" and fakes.names[-1] == "digest"


def test_unexpected_error_in_one_stage_is_partial_in_every_stage_failed():
    one = run_pass([domain()], OFF, Fakes(collect=KeyError("boom")).boundaries(), NOW, send=False)
    assert one.status == "partial"
    every = Fakes(collect=KeyError("a"), triage=KeyError("b"), embed=KeyError("c"), digest=KeyError("d"))
    assert run_pass([domain()], ON, every.boundaries(), NOW, send=False).status == "failed"


def test_failed_send_is_partial():
    fakes = Fakes(digest=DigestOutcome("failed", "08:30", "2026-11-08", 2, ["x"], "flood wait"))
    report = run_pass([domain()], OFF, fakes.boundaries(), NOW, send=True)
    assert report.status == "partial" and "flood wait" in report.errors[0]


def test_explain_mentions_disabled_triage():
    report = run_pass([], OFF, Fakes().boundaries(), NOW, send=False)
    text = "\n".join(explain(report, OFF))
    assert "triage is disabled (triage.enabled: false)" in text
    assert "no enabled domains" in text
    assert "digest preview only" in text and "notify.enabled" in text


def test_real_collect_boundary_builds_no_client_when_nothing_is_due(tmp_path, monkeypatch):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    repo.set_domain_scanned(conn, "alpha", "2026-11-08T07:00:00Z", "ok", "h", 3600)
    monkeypatch.setattr(pipeline.collect_cli, "make_client", lambda *a: pytest.fail("no client when nothing is due"))
    b = pipeline.build_boundaries(conn, OFF, [domain()])
    assert b.collect([domain()], NOW).domains == []
    b.close()


# ---- CLI ---------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def preflight_ok(monkeypatch):
    """`repka run` preflight (token, GET /user, models) is covered in test_preflight; here it always passes."""
    monkeypatch.setattr(preflight, "check", lambda *a, **k: [])


def write_domain(home, domain_id="alpha", enabled="true"):
    (home / "domains").mkdir(exist_ok=True)
    (home / "domains" / f"{domain_id}.yaml").write_text(
        f"id: {domain_id}\nname: {domain_id}\nenabled: {enabled}\nprofile: p\nsearch:\n  topics: [mcp]\n"
    )


def patch_boundaries(monkeypatch, fakes: Fakes, seen: list | None = None):
    def build(conn, cfg, domains, **kwargs):
        if seen is not None:
            seen.append(([d.id for d in domains], kwargs))
        return fakes.boundaries()

    monkeypatch.setattr(pipeline, "build_boundaries", build)


def test_send_requires_notify_enabled(home, capsys):
    write_domain(home)
    assert main(["run", "--send"]) == 2
    assert "notify.enabled" in capsys.readouterr().err
    assert not (home / ".state" / "repka.db").exists()  # refused before any work


def test_lock_busy_exits_zero(home, monkeypatch, capsys):
    write_domain(home)
    patch_boundaries(monkeypatch, Fakes(collect=AssertionError("must not run")))
    with RunLock(home / ".state" / "run.lock"):
        assert main(["run"]) == 0
    assert "another repka run is in progress" in capsys.readouterr().err


def test_runs_table_records_status_and_stats(home, monkeypatch, capsys):
    write_domain(home)
    (home / ".config.yaml").write_text("triage:\n  enabled: true\n")
    patch_boundaries(monkeypatch, Fakes(triage=WorkerError("worker timed out")))
    assert main(["run", "--json"]) == 0  # partial still exits 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "partial" and out["send"] is False and out["digest"]["parts"] == ["x"]
    assert not any("triage is disabled" in h for h in out["hints"]) and out["errors"]
    conn = connect(home / ".state" / "repka.db")
    row = conn.execute("SELECT kind, status, stats_json, error, finished_at FROM runs").fetchone()
    conn.close()
    assert (row["kind"], row["status"]) == ("run", "partial") and row["finished_at"]
    stats = json.loads(row["stats_json"])
    assert stats["digest"]["status"] == "preview" and "parts" not in stats["digest"]
    assert "worker timed out" in row["error"]


def test_failed_run_exits_one_and_is_recorded(home, monkeypatch, capsys):
    write_domain(home)
    patch_boundaries(monkeypatch, Fakes(collect=KeyError("a"), digest=KeyError("b")))
    assert main(["run"]) == 1
    conn = connect(home / ".state" / "repka.db")
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "failed"
    conn.close()
    assert "run #1: failed" in capsys.readouterr().out


def test_domain_filter_limits_all_stages(home, monkeypatch, capsys):
    write_domain(home, "alpha")
    write_domain(home, "beta")
    (home / ".config.yaml").write_text("triage:\n  enabled: true\nembed:\n  enabled: true\n")
    fakes, seen = Fakes(), []
    patch_boundaries(monkeypatch, fakes, seen)
    assert main(["run", "--domain", "beta"]) == 0
    assert [c[:2] for c in fakes.calls if c[0] != "digest"] == [
        ("collect", ["beta"]), ("triage", "beta"), ("embed", "beta")
    ]
    assert seen[0][0] == ["beta"] and seen[0][1]["force"] is True  # the digest covers only beta; collect is forced
    assert "preview" in capsys.readouterr().out
    assert main(["run", "--domain", "nope"]) == 2
    write_domain(home, "off", enabled="false")
    assert main(["run", "--domain", "off"]) == 2
