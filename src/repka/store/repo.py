"""Data access for the repka store. Every function takes `conn` first and never commits."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from repka.models import Event, KeyFile, RepoHit, Snapshot
from repka.store.db import utcnow


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def upsert_repo(conn: sqlite3.Connection, hit: RepoHit, now: str) -> bool:
    """Insert or update a repo keyed by GitHub id. Returns True if the row is new."""
    exists = conn.execute("SELECT 1 FROM repos WHERE repo_id = ?", (hit.repo_id,)).fetchone()
    conn.execute(
        """
        INSERT INTO repos (repo_id, node_id, full_name, html_url, description, language, topics_json,
                           license, is_fork, created_at, pushed_at, archived,
                           first_seen_at, last_seen_at, status)
        VALUES (:repo_id, :node_id, :full_name, :html_url, :description, :language, :topics_json,
                :license, :is_fork, :created_at, :pushed_at, :archived, :now, :now, 'active')
        ON CONFLICT (repo_id) DO UPDATE SET
            node_id = excluded.node_id,
            full_name = excluded.full_name,
            html_url = excluded.html_url,
            description = excluded.description,
            language = excluded.language,
            topics_json = excluded.topics_json,
            license = excluded.license,
            is_fork = excluded.is_fork,
            created_at = excluded.created_at,
            pushed_at = excluded.pushed_at,
            archived = excluded.archived,
            last_seen_at = excluded.last_seen_at,
            status = 'active'
        """,
        {
            "repo_id": hit.repo_id,
            "node_id": hit.node_id,
            "full_name": hit.full_name,
            "html_url": hit.html_url,
            "description": hit.description,
            "language": hit.language,
            "topics_json": json.dumps(list(hit.topics)),
            "license": hit.license,
            "is_fork": int(hit.is_fork),
            "created_at": _iso(hit.created_at),
            "pushed_at": _iso(hit.pushed_at),
            "archived": int(hit.archived),
            "now": now,
        },
    )
    return exists is None


def link_domain_repo(conn: sqlite3.Connection, domain_id: str, repo_id: int, query_key: str, now: str) -> None:
    """Record that a repo matched a domain through `query_key` (keys accumulate, no duplicates)."""
    row = conn.execute(
        "SELECT query_keys_json FROM domain_repos WHERE domain_id = ? AND repo_id = ?",
        (domain_id, repo_id),
    ).fetchone()
    keys: list[str] = json.loads(row["query_keys_json"]) if row and row["query_keys_json"] else []
    if query_key not in keys:
        keys.append(query_key)
    conn.execute(
        """
        INSERT INTO domain_repos (domain_id, repo_id, first_matched_at, last_matched_at, query_keys_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (domain_id, repo_id) DO UPDATE SET
            last_matched_at = excluded.last_matched_at,
            query_keys_json = excluded.query_keys_json
        """,
        (domain_id, repo_id, now, now, json.dumps(keys)),
    )


def domain_has_repo(conn: sqlite3.Connection, domain_id: str, repo_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM domain_repos WHERE domain_id = ? AND repo_id = ?", (domain_id, repo_id)).fetchone()
    return row is not None


def mark_repo_gone(conn: sqlite3.Connection, repo_id: int) -> None:
    conn.execute("UPDATE repos SET status = 'gone' WHERE repo_id = ?", (repo_id,))


def insert_snapshot(conn: sqlite3.Connection, s: Snapshot) -> None:
    conn.execute(
        """
        INSERT INTO snapshots (repo_id, taken_at, stars, forks, open_issues, watchers, contributors,
                               commits_7d, commits_30d, active_weeks_12, releases_90d,
                               latest_release_tag, latest_release_at)
        VALUES (:repo_id, :taken_at, :stars, :forks, :open_issues, :watchers, :contributors,
                :commits_7d, :commits_30d, :active_weeks_12, :releases_90d,
                :latest_release_tag, :latest_release_at)
        """,
        {
            "repo_id": s.repo_id,
            "taken_at": _iso(s.taken_at),
            "stars": s.stars,
            "forks": s.forks,
            "open_issues": s.open_issues,
            "watchers": s.watchers,
            "contributors": s.contributors,
            "commits_7d": s.commits_7d,
            "commits_30d": s.commits_30d,
            "active_weeks_12": s.active_weeks_12,
            "releases_90d": s.releases_90d,
            "latest_release_tag": s.latest_release_tag,
            "latest_release_at": _iso(s.latest_release_at),
        },
    )


def _snapshot_from_row(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        repo_id=row["repo_id"],
        taken_at=_parse(row["taken_at"]),
        stars=row["stars"],
        forks=row["forks"],
        open_issues=row["open_issues"],
        watchers=row["watchers"],
        contributors=row["contributors"],
        commits_7d=row["commits_7d"],
        commits_30d=row["commits_30d"],
        active_weeks_12=row["active_weeks_12"],
        releases_90d=row["releases_90d"],
        latest_release_tag=row["latest_release_tag"],
        latest_release_at=_parse(row["latest_release_at"]),
    )


def latest_snapshot(conn: sqlite3.Connection, repo_id: int) -> Snapshot | None:
    row = conn.execute(
        "SELECT * FROM snapshots WHERE repo_id = ? ORDER BY taken_at DESC LIMIT 1", (repo_id,)
    ).fetchone()
    return _snapshot_from_row(row) if row else None


def snapshots_for(conn: sqlite3.Connection, repo_id: int, since: str | None = None) -> list[Snapshot]:
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE repo_id = ? AND (? IS NULL OR taken_at >= ?) ORDER BY taken_at",
        (repo_id, since, since),
    ).fetchall()
    return [_snapshot_from_row(r) for r in rows]


def latest_key_file(conn: sqlite3.Connection, repo_id: int, path: str) -> tuple[str, list[str] | None] | None:
    row = conn.execute(
        """
        SELECT blob_oid, deps_json FROM key_files
        WHERE repo_id = ? AND path = ? ORDER BY first_seen_at DESC, rowid DESC LIMIT 1
        """,
        (repo_id, path),
    ).fetchone()
    if row is None:
        return None
    deps = json.loads(row["deps_json"]) if row["deps_json"] is not None else None
    return row["blob_oid"], deps


def insert_key_file(conn: sqlite3.Connection, kf: KeyFile, deps: list[str] | None, now: str) -> None:
    """Record a blob. Re-seeing an old oid (a revert) bumps its first_seen_at so it stays the latest."""
    conn.execute(
        """
        INSERT INTO key_files (repo_id, path, blob_oid, deps_json, first_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (repo_id, path, blob_oid) DO UPDATE SET
            deps_json = excluded.deps_json,
            first_seen_at = excluded.first_seen_at
        """,
        (kf.repo_id, kf.path, kf.blob_oid, None if deps is None else json.dumps(deps), now),
    )


def add_event(conn: sqlite3.Connection, e: Event) -> int:
    cur = conn.execute(
        """
        INSERT INTO events (repo_id, domain_id, kind, payload_json, detected_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (e.repo_id, e.domain_id, e.kind, json.dumps(e.payload, sort_keys=True), _iso(e.detected_at)),
    )
    return int(cur.lastrowid)


def cursor_done(conn: sqlite3.Connection, domain_id: str, query_key: str, slice_key: str) -> bool:
    row = conn.execute(
        "SELECT completed_at FROM search_cursor WHERE domain_id = ? AND query_key = ? AND slice = ?",
        (domain_id, query_key, slice_key),
    ).fetchone()
    return row is not None and row["completed_at"] is not None


def mark_cursor(
    conn: sqlite3.Connection,
    domain_id: str,
    query_key: str,
    slice_key: str,
    total_count: int,
    capped: bool,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO search_cursor (domain_id, query_key, slice, completed_at, total_count, capped)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (domain_id, query_key, slice) DO UPDATE SET
            completed_at = excluded.completed_at,
            total_count = excluded.total_count,
            capped = excluded.capped
        """,
        (domain_id, query_key, slice_key, now, total_count, int(capped)),
    )


@dataclass(frozen=True)
class DomainState:
    domain_id: str
    last_scan_at: str | None
    last_scan_status: str | None


def get_domain_state(conn: sqlite3.Connection, domain_id: str) -> DomainState:
    row = conn.execute(
        "SELECT last_scan_at, last_scan_status FROM domains WHERE id = ?", (domain_id,)
    ).fetchone()
    if row is None:
        return DomainState(domain_id, None, None)
    return DomainState(domain_id, row["last_scan_at"], row["last_scan_status"])


def set_domain_scanned(
    conn: sqlite3.Connection,
    domain_id: str,
    at: str,
    status: str,
    profile_hash: str,
    scan_every_s: int,
) -> None:
    conn.execute(
        """
        INSERT INTO domains (id, profile_hash, scan_every_s, last_scan_at, last_scan_status)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            profile_hash = excluded.profile_hash,
            scan_every_s = excluded.scan_every_s,
            last_scan_at = excluded.last_scan_at,
            last_scan_status = excluded.last_scan_status
        """,
        (domain_id, profile_hash, scan_every_s, at, status),
    )


def repos_for_domain(conn: sqlite3.Connection, domain_id: str, accepted_only: bool = False) -> list[sqlite3.Row]:
    """Repos matched to a domain. `accepted` is the latest verdict's accepted flag (NULL if undecided)."""
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT r.*, dr.first_matched_at, dr.last_matched_at, dr.query_keys_json,
                   (SELECT v.accepted FROM verdicts v
                     WHERE v.repo_id = dr.repo_id AND v.domain_id = dr.domain_id
                     ORDER BY v.decided_at DESC, v.rowid DESC LIMIT 1) AS accepted
            FROM domain_repos dr
            JOIN repos r ON r.repo_id = dr.repo_id
            WHERE dr.domain_id = ?
        )
        WHERE ? = 0 OR accepted = 1
        ORDER BY repo_id
        """,
        (domain_id, int(accepted_only)),
    ).fetchall()
    return list(rows)


def start_run(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, utcnow()))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, stats: dict[str, Any], error: str | None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, stats_json = ?, error = ? WHERE id = ?",
        (utcnow(), status, json.dumps(stats, sort_keys=True), error, run_id),
    )


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name"
        )
    ]
    return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names}


class SqliteEtagCache:
    """http_cache-backed ETag cache (matches github.client.EtagCache)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, url: str) -> tuple[str, str] | None:
        row = self._conn.execute("SELECT etag, body FROM http_cache WHERE url = ?", (url,)).fetchone()
        return (row["etag"], row["body"]) if row else None

    def put(self, url: str, etag: str, body: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO http_cache (url, etag, body, fetched_at) VALUES (?, ?, ?, ?)",
            (url, etag, body, utcnow()),
        )
