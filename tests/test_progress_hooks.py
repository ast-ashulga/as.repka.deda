import io
import json
import sys
import textwrap
from datetime import UTC, datetime

import pytest

import repka.collect as collect_mod
from helpers_digest import add_repo, add_verdict, make_db
from repka.config import AppConfig
from repka.domain.schema import Domain, SearchSpec
from repka.embed.embed import embed_domain
from repka.github.enrich import EnrichStats
from repka.github.search import QueryStats
from repka.progress.active import activate
from repka.progress.render import JsonlRenderer
from repka.store.db import connect, migrate
from repka.triage import triage as triage_mod
from repka.triage.deciders import ModelSpec, run_worker
from test_embed import FakeEmbedder
from test_triage import DOMAIN, THRESHOLDS, FakeBackend, questions

NOW = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)


class Rec:
    """A Reporter that records every call."""

    def __init__(self):
        self.calls = []

    def plan(self, stages):
        self.calls.append(("plan", stages))

    def stage_start(self, stage, steps=None):
        self.calls.append(("stage_start", stage, steps))

    def step(self, label, unit="", total=None):
        self.calls.append(("step", label, unit, total))

    def advance(self, done, total=None, detail=""):
        self.calls.append(("advance", done, total))

    def stage_end(self, status, detail="", items=None):
        self.calls.append(("stage_end", status))

    def note(self, message):
        self.calls.append(("note", message))

    def finish(self, status):
        self.calls.append(("finish", status))


# ---- collect ------------------------------------------------------------------------------------


def collect_domain():
    return Domain(id="alpha", name="alpha", profile="p", search=SearchSpec(keywords=(), topics=("mcp",), languages=(), exclude=()))


def run_collect(tmp_path, monkeypatch, reporter):
    conn = connect(tmp_path / "c.db")
    migrate(conn)
    messages = []

    def fake_search(client, conn, domain, window, now, max_repos, emit_new, *, per_page, progress=None):
        for i in (1, 2, 3):
            progress(i, 3, None, QueryStats(hits=2))
        return QueryStats(hits=6), set()

    def fake_enrich(client, conn, domain_id, repo_ids, key_files, now, cfg, slow_for, baseline=False, progress=None, **kw):
        progress(50, 120)
        progress(100, 120)
        return EnrichStats(repos=120)

    monkeypatch.setattr(collect_mod, "collect_search", fake_search)
    monkeypatch.setattr(collect_mod, "enrich_repos", fake_enrich)
    monkeypatch.setattr(collect_mod, "_enrich_targets", lambda conn, did: (list(range(120)), set()))
    kwargs = {} if reporter is None else {"reporter": reporter}
    collect_mod.collect_domains(object(), conn, [collect_domain()], AppConfig(), NOW, progress=messages.append, **kwargs)
    conn.close()
    return messages


def test_collect_emits_steps_and_advances_and_keeps_string_messages(tmp_path, monkeypatch):
    rec = Rec()
    messages = run_collect(tmp_path, monkeypatch, rec)
    assert [c for c in rec.calls if c[0] == "step"][:2] == [
        ("step", "alpha search", "queries", rec.calls[0][3]),
        ("step", "alpha enrich", "repos", 120),
    ]
    assert [c[1:] for c in rec.calls if c[0] == "advance"] == [(1, 3), (2, 3), (3, 3), (50, 120), (100, 120)]
    assert any("query 2/3 done" in m for m in messages) and any("enriched 100/120" in m for m in messages)


def test_collect_without_reporter_is_unchanged(tmp_path, monkeypatch):
    messages = run_collect(tmp_path, monkeypatch, None)
    assert any("enriched 50/120" in m for m in messages)


# ---- triage ---------------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = make_db(tmp_path / "t.db")
    yield c
    c.close()


def seed(conn, n):
    for i in range(1, n + 1):
        add_repo(conn, i)


def run_triage(conn, backend, on_progress=None):
    return triage_mod.triage_domain(
        conn, DOMAIN, backend, questions(), "m", THRESHOLDS, lambda name: f"readme {name}", 4000,
        "2026-11-07T10:00:00Z", None, on_progress,
    )  # fmt: skip


def test_triage_progress_per_chunk(conn):
    seed(conn, 450)
    seen = []
    run_triage(conn, FakeBackend(), lambda done, total, detail: seen.append((done, total, detail)))
    assert seen[0] == (0, 450, "")
    chunk_ends = [done for done, _, detail in seen if detail == ""]
    assert chunk_ends == [0, 200, 400, 450]
    assert any(d.startswith("readme ") for _, _, d in seen) and any(d.startswith("worker deciding") for _, _, d in seen)
    assert all(t == 450 for _, t, _ in seen)


def test_triage_progress_counts_a_chunk_where_every_repo_fails(conn):
    seed(conn, 3)
    seen = []

    def boom(name):
        from repka.github.client import GitHubError

        raise GitHubError("nope")

    triage_mod.triage_domain(
        conn, DOMAIN, FakeBackend(), questions(), "m", THRESHOLDS, boom, 4000, "2026-11-07T10:00:00Z", None,
        lambda done, total, detail: seen.append((done, detail)),
    )  # fmt: skip
    assert (3, "") in seen  # a chunk with nothing to decide still advances


def test_triage_without_callback_and_with_a_broken_callback(conn):
    seed(conn, 3)
    assert run_triage(conn, FakeBackend()).decided == 3
    seed_more = 2
    for i in range(10, 10 + seed_more):
        add_repo(conn, i)

    def broken(done, total, detail):
        raise RuntimeError("renderer exploded")

    assert run_triage(conn, FakeBackend(), broken).decided == seed_more  # the stage is not affected


# ---- embed ----------------------------------------------------------------------------------------


def test_embed_progress_per_batch(conn):
    for i in range(1, 6):
        add_repo(conn, i)
        add_verdict(conn, i, accepted=True)
    seen = []
    stats = embed_domain(conn, "d1", FakeEmbedder(), 2, "2026-11-07T09:00:00Z", lambda *a: seen.append(a))
    assert stats.embedded == 5
    assert seen == [(0, 5, ""), (2, 5, ""), (4, 5, ""), (5, 5, "")]


# ---- worker output relay -------------------------------------------------------------------------

WORKER = textwrap.dedent(
    """
    import argparse, json, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--config"); ap.add_argument("--requests"); ap.add_argument("--out")
    a = ap.parse_args()
    print("loading model weights")
    print("warming up", file=sys.stderr)
    json.dump({"results": [], "meta": {}}, open(a.out, "w"))
    """
)


@pytest.fixture
def worker_dir(tmp_path):
    d = tmp_path / "deciders"
    (d / "venv" / "bin").mkdir(parents=True)
    (d / "venv" / "bin" / "python").symlink_to(sys.executable)
    (d / "workers").mkdir()
    (d / "workers" / "chatty.py").write_text(WORKER)
    return d


def chatty_spec():
    return ModelSpec("chatty", "chatty", "venv", "r", "a" * 40, "joint", "auto", "auto", False)  # type: ignore[arg-type]


def test_run_worker_relays_output_when_stderr_is_owned(worker_dir, capfd):
    out = io.StringIO()
    renderer = JsonlRenderer(out, lambda: 0.0, lambda: 1_700_000_000.0)
    with activate(renderer):
        run_worker(chatty_spec(), [], deciders_dir=worker_dir, timeout_s=60, online=False)
    objs = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert {o["message"] for o in objs} == {"loading model weights", "warming up"}
    assert all(o["event"] == "log" for o in objs)
    assert "loading model weights" not in capfd.readouterr().err  # not written under the renderer


def test_run_worker_inherits_stderr_fd_otherwise(worker_dir, capfd):
    run_worker(chatty_spec(), [], deciders_dir=worker_dir, timeout_s=60, online=False)
    err = capfd.readouterr().err
    assert "loading model weights" in err and "warming up" in err
