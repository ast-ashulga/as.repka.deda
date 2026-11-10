from datetime import UTC, datetime

from repka.domain.schema import load_domain
from repka.models import RepoHit, Snapshot
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
