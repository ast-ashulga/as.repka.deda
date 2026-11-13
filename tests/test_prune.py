from datetime import UTC, datetime

import pytest

from repka.cli import main
from repka.config import state_dir
from repka.domain.schema import load_domain
from repka.models import RepoHit, Snapshot
from repka.runlock import LockBusy, RunLock
from repka.store import prune as prune_mod
from repka.store import repo
from repka.store.db import connect, migrate
from repka.store.prune import prune

NOW = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


def _hit(rid: int, name: str) -> RepoHit:
    return RepoHit(
        repo_id=rid, node_id=f"R_{rid}", full_name=name, html_url=f"https://github.com/{name}", description="d",
        language="Python", topics=(), stars=10, forks=0, is_fork=False, created_at=NOW, pushed_at=NOW,
        archived=False, license=None,
    )  # fmt: skip


def _snap(rid: int, stars: int) -> Snapshot:
    return Snapshot(
        repo_id=rid, taken_at=NOW, stars=stars, forks=0, open_issues=0, watchers=0, contributors=None,
        commits_7d=0, commits_30d=0, active_weeks_12=None, releases_90d=0, latest_release_tag=None,
        latest_release_at=None,
    )  # fmt: skip


def test_prune_drops_low_star_repos_and_cuts_readmes(tmp_path):
    domain_file = tmp_path / "dd.yaml"
    domain_file.write_text(
        "id: dd\nname: D\nprofile: p\nsearch:\n  keywords: [[x]]\n  min_stars: 300\n", encoding="utf-8"
    )
    domain = load_domain(domain_file)
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    for rid, name, stars in [(1, "a/low", 50), (2, "a/high", 500)]:
        repo.upsert_repo(conn, _hit(rid, name), "2026-11-01T08:00:00Z")
        repo.link_domain_repo(conn, "dd", rid, "k", "2026-11-01T08:00:00Z")
        repo.insert_snapshot(conn, _snap(rid, stars))
        conn.execute(
            "INSERT INTO http_cache (url, etag, body, fetched_at) VALUES (?, 'e', ?, 't')",
            (f"https://api.github.com/repos/{name}/readme", "x" * 100),
        )
    conn.commit()

    dry = prune(conn, [domain], 10, apply=False)
    assert dry.below_min_stars == {"dd": 1} and dry.repos_deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 2

    done = prune(conn, [domain], 10, apply=True)
    assert done.repos_deleted == 1 and done.cache_rows_deleted == 1 and done.readme_bodies_cut == 1
    assert [r[0] for r in conn.execute("SELECT full_name FROM repos")] == ["a/high"]
    assert conn.execute("SELECT length(body) FROM http_cache").fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


def test_prune_thins_old_snapshots_to_one_per_week(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    repo.upsert_repo(conn, _hit(1, "a/x"), "2026-01-01T00:00:00Z")
    repo.link_domain_repo(conn, "dd", 1, "k", "2026-01-01T00:00:00Z")  # held by a domain: not an orphan
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-13", "2026-10-30", "2026-10-31"]  # 3 in one ISO week
    for day in days:
        conn.execute(
            "INSERT INTO snapshots (repo_id, taken_at, stars, forks, open_issues, watchers, commits_7d, commits_30d,"
            " releases_90d) VALUES (1, ?, 10, 0, 0, 0, 0, 0, 0)",
            (f"{day}T08:00:00Z",),
        )
    conn.commit()
    now = datetime(2026, 11, 1, tzinfo=UTC)
    report = prune(conn, [], 4000, apply=True, snapshot_days=90, now=now)
    kept = [r[0][:10] for r in conn.execute("SELECT taken_at FROM snapshots ORDER BY taken_at")]
    assert report.snapshots_thinned == 2
    assert kept == ["2026-01-07", "2026-01-13", "2026-10-30", "2026-10-31"]
    assert prune(conn, [], 4000, apply=True, snapshot_days=0, now=now).snapshots_thinned == 0


@pytest.fixture
def db_home(home):
    conn = connect(state_dir() / "repka.db")
    migrate(conn)
    conn.close()
    return home


@pytest.mark.parametrize("argv", [["db", "prune"], ["db", "prune", "--apply"]])
def test_prune_always_runs_under_the_run_lock(db_home, monkeypatch, argv):
    """A dry run also opens a writable connection and migrates, so it must not run beside a live `repka run`."""
    held = []

    def spy(*a, **k):
        try:
            with RunLock(state_dir() / "run.lock"):
                held.append(False)
        except LockBusy:
            held.append(True)
        return prune_mod.PruneReport()

    monkeypatch.setattr(prune_mod, "prune", spy)
    assert main(argv) == 0
    assert held == [True]


def test_prune_dry_run_leaves_the_database_alone_while_a_run_holds_the_lock(db_home, monkeypatch, capsys):
    monkeypatch.setattr(prune_mod, "connect", lambda *a, **k: pytest.fail("opened the database without the lock"))
    with RunLock(state_dir() / "run.lock"):
        assert main(["db", "prune"]) == 0
    cap = capsys.readouterr()
    assert "another repka run is in progress" in cap.err and "would prune" not in cap.out


def test_snapshot_days_zero_message_says_thinning_is_off(home, capsys):
    from repka.cli import main
    from repka.store.db import connect, migrate

    conn = connect()
    migrate(conn)
    conn.close()
    assert main(["db", "prune", "--snapshot-days", "0"]) == 0
    out = capsys.readouterr().out
    assert "thinning is off" in out and "thinned to one per week" not in out
    assert main(["db", "prune"]) == 0
    assert "thinned to one per week" in capsys.readouterr().out
