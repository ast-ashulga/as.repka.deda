from datetime import UTC, datetime
import sqlite3

import pytest

from repka.cli import main
from repka.models import Event, KeyFile, RepoHit, Snapshot
from repka.store import repo
from repka.store import db
from repka.store.db import connect, migrate, utcnow
from repka.store.repo import DomainState, SqliteEtagCache

T0 = "2026-11-01T08:00:00Z"
T1 = "2026-11-02T08:00:00Z"
DT0 = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


def _hit(full_name: str = "octo/cat", repo_id: int = 42) -> RepoHit:
    return RepoHit(
        repo_id=repo_id,
        node_id=f"R_{repo_id}",
        full_name=full_name,
        html_url=f"https://github.com/{full_name}",
        description="a cat",
        language="Python",
        topics=("cli", "ai"),
        stars=10,
        forks=2,
        is_fork=False,
        created_at=DT0,
        pushed_at=DT0,
        archived=False,
        license="MIT",
    )


def _snap(repo_id: int = 42, stars: int = 10, taken_at: datetime = DT0) -> Snapshot:
    return Snapshot(
        repo_id=repo_id,
        taken_at=taken_at,
        stars=stars,
        forks=2,
        open_issues=3,
        watchers=4,
        contributors=None,
        commits_7d=1,
        commits_30d=5,
        active_weeks_12=6,
        releases_90d=0,
        latest_release_tag=None,
        latest_release_at=None,
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    migrate(c)
    yield c
    c.close()


def test_migrate_creates_tables_and_is_idempotent(tmp_path):
    c = connect(tmp_path / "m.db")
    v1 = migrate(c)
    v2 = migrate(c)
    assert v1 == v2 == max(n for n, _ in db._migrations())
    assert "repos" in repo.table_counts(c)
    c.close()


def test_upsert_new_then_existing(conn):
    assert repo.upsert_repo(conn, _hit(), T0) is True
    assert repo.upsert_repo(conn, _hit(), T1) is False


def test_upsert_rename_keeps_history(conn):
    repo.upsert_repo(conn, _hit("old/name"), T0)
    repo.insert_snapshot(conn, _snap())
    assert repo.upsert_repo(conn, _hit("new/name"), T1) is False
    row = conn.execute("SELECT full_name FROM repos WHERE repo_id = 42").fetchone()
    assert row["full_name"] == "new/name"
    assert repo.table_counts(conn)["repos"] == 1
    assert repo.latest_snapshot(conn, 42) == _snap()


def test_mark_gone_keeps_snapshots(conn):
    repo.upsert_repo(conn, _hit(), T0)
    repo.insert_snapshot(conn, _snap())
    repo.mark_repo_gone(conn, 42)
    status = conn.execute("SELECT status FROM repos WHERE repo_id = 42").fetchone()["status"]
    assert status == "gone"
    assert repo.latest_snapshot(conn, 42) == _snap()


def test_cursor_roundtrip(conn):
    assert repo.cursor_done(conn, "d1", "q1", "2026-11-01..2026-11-01") is False
    repo.mark_cursor(conn, "d1", "q1", "2026-11-01..2026-11-01", 1500, True, T0)
    assert repo.cursor_done(conn, "d1", "q1", "2026-11-01..2026-11-01") is True


def test_domain_state_defaults_none(conn):
    assert repo.get_domain_state(conn, "nope") == DomainState("nope", None, None)
    repo.set_domain_scanned(conn, "d1", T0, "ok", "hash", 3600)
    assert repo.get_domain_state(conn, "d1") == DomainState("d1", T0, "ok")


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_snapshot(conn, _snap(repo_id=999))


def test_readonly_connection_rejects_writes(tmp_path):
    path = tmp_path / "ro.db"
    c = connect(path)
    migrate(c)
    c.close()
    ro = connect(path, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO runs (kind) VALUES ('x')")
    ro.close()


def test_key_file_and_event_and_etag_roundtrip(conn):
    repo.upsert_repo(conn, _hit(), T0)
    assert repo.latest_key_file(conn, 42, "go.mod") is None
    repo.insert_key_file(conn, KeyFile(42, "go.mod", "oid1", None), ["a", "b"], T0)
    assert repo.latest_key_file(conn, 42, "go.mod") == ("oid1", ["a", "b"])
    event_id = repo.add_event(conn, Event(42, "d1", "release", {"tag": "v1"}, DT0))
    assert isinstance(event_id, int)
    cache = SqliteEtagCache(conn)
    assert cache.get("https://x") is None
    cache.put("https://x", '"e1"', "{}")
    assert cache.get("https://x") == ('"e1"', "{}")
    assert utcnow().endswith("Z")


def test_db_stats_cli(home, capsys):
    assert main(["db", "migrate"]) == 0
    capsys.readouterr()
    assert main(["db", "stats"]) == 0
    out = capsys.readouterr().out
    assert "repos 0" in out
    assert (home / ".state" / "repka.db").exists()
