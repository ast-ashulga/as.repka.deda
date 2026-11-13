"""`repka db prune [--apply]`: drop stored data that the current domain filters no longer match.

What it does (dry run unless --apply):
  1. For every domain with `search.min_stars > 0`, repos whose latest snapshot has fewer stars leave
     the domain (their `domain_repos` row and that domain's verdicts go). Repos with no snapshot stay.
  2. Repos that no domain holds any more are deleted with their snapshots, key files, events, verdicts,
     embeddings and cached HTTP bodies. Repos mentioned in a sent or built digest (`digest_items`) are
     kept, so digest history stays readable.
  3. Cached README bodies in `http_cache` are cut to `triage.readme_chars` (the ETag stays, so a 304
     still works).
  4. Snapshots older than --snapshot-days (default 90) are thinned to the last one of each calendar week per
     repo (weeks start on Monday, as SQLite's %W counts them, and restart each January 1); newer snapshots
     and every repo's latest snapshot are never touched. 0 turns this off.
  5. With --apply the file is VACUUMed so the space is returned to the disk.
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from repka.config import ConfigError, db_path, load_config
from repka.domain.schema import Domain, load_domains
from repka.runlock import run_exclusive
from repka.store.db import connect, migrate

API_PREFIX = "https://api.github.com/repos/"

_LATEST_STARS = """
    SELECT s.stars FROM snapshots s WHERE s.repo_id = dr.repo_id ORDER BY s.taken_at DESC LIMIT 1
"""


@dataclass
class PruneReport:
    below_min_stars: dict[str, int] = field(default_factory=dict)  # domain id -> repos leaving it
    repos_deleted: int = 0
    cache_rows_deleted: int = 0
    readme_bodies_cut: int = 0
    snapshots_thinned: int = 0


def _leaving(conn: sqlite3.Connection, domain: Domain) -> list[int]:
    if domain.search.min_stars <= 0:
        return []
    rows = conn.execute(
        f"""
        SELECT dr.repo_id FROM domain_repos dr
        WHERE dr.domain_id = ?
          AND ({_LATEST_STARS}) < ?
          AND dr.repo_id NOT IN (SELECT repo_id FROM digest_items)
        """,
        (domain.id, domain.search.min_stars),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _orphans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT repo_id, full_name FROM repos
        WHERE repo_id NOT IN (SELECT repo_id FROM domain_repos)
          AND repo_id NOT IN (SELECT repo_id FROM digest_items)
        """
    ).fetchall()


DEFAULT_SNAPSHOT_DAYS = 90


def prune(
    conn: sqlite3.Connection,
    domains: list[Domain],
    readme_chars: int,
    apply: bool,
    snapshot_days: int = DEFAULT_SNAPSHOT_DAYS,
    now: datetime | None = None,
) -> PruneReport:
    """Run inside one transaction; roll back on a dry run so the counts are exact but nothing changes."""
    report = PruneReport()
    try:
        for domain in domains:
            ids = _leaving(conn, domain)
            if not ids:
                continue
            report.below_min_stars[domain.id] = len(ids)
            for chunk in _chunks(ids):
                marks = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM verdicts WHERE domain_id = ? AND repo_id IN ({marks})", (domain.id, *chunk))
                conn.execute(f"DELETE FROM domain_repos WHERE domain_id = ? AND repo_id IN ({marks})", (domain.id, *chunk))
        orphans = _orphans(conn)
        report.repos_deleted = len(orphans)
        for row in orphans:
            repo_id = int(row["repo_id"])
            for table in ("snapshots", "key_files", "events", "verdicts", "embeddings"):
                conn.execute(f"DELETE FROM {table} WHERE repo_id = ?", (repo_id,))
            prefix = f"{API_PREFIX}{row['full_name']}/"
            cur = conn.execute("DELETE FROM http_cache WHERE substr(url, 1, ?) = ?", (len(prefix), prefix))
            report.cache_rows_deleted += cur.rowcount
            conn.execute("DELETE FROM repos WHERE repo_id = ?", (repo_id,))
        cur = conn.execute(
            "UPDATE http_cache SET body = substr(body, 1, ?) WHERE url LIKE ? AND length(body) > ?",
            (readme_chars, f"{API_PREFIX}%/readme", readme_chars),
        )
        report.readme_bodies_cut = cur.rowcount
        if snapshot_days > 0:
            cutoff = ((now or datetime.now(UTC)) - timedelta(days=snapshot_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            cur = conn.execute(
                """
                DELETE FROM snapshots WHERE taken_at < ? AND (repo_id, taken_at) NOT IN (
                    SELECT repo_id, MAX(taken_at) FROM snapshots GROUP BY repo_id, strftime('%Y-%W', taken_at)
                )
                """,
                (cutoff,),
            )
            report.snapshots_thinned = cur.rowcount
        if apply:
            conn.commit()
        else:
            conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    return report


def _chunks(ids: list[int], size: int = 500) -> list[list[int]]:  # stay under SQLite's variable limit
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _run(args: argparse.Namespace) -> int:
    path = db_path()
    if not path.exists():
        raise ConfigError(f"no database at {path}; run `repka db migrate` first")
    cfg = load_config()
    domains = load_domains()
    conn = connect()
    try:
        migrate(conn)
        before = path.stat().st_size
        report = prune(conn, domains, cfg.triage.readme_chars, args.apply, args.snapshot_days)
        if args.apply:
            conn.execute("VACUUM")
    finally:
        conn.close()
    mode = "pruned" if args.apply else "would prune (dry run; pass --apply to do it)"
    print(mode)
    for domain_id, count in sorted(report.below_min_stars.items()):
        print(f"  domain {domain_id}: {count} repos below min_stars leave the domain")
    print(f"  repos deleted (held by no domain, not in a digest): {report.repos_deleted}")
    print(f"  http_cache rows of those repos deleted: {report.cache_rows_deleted}")
    print(f"  cached README bodies cut to {cfg.triage.readme_chars} chars: {report.readme_bodies_cut}")
    if args.snapshot_days:
        print(f"  snapshots older than {args.snapshot_days} days thinned to one per week: {report.snapshots_thinned}")
    else:
        print("  snapshot thinning is off (--snapshot-days 0): every snapshot is kept")
    if args.apply:
        print(f"  file size: {before / 1048576:.1f} MB -> {path.stat().st_size / 1048576:.1f} MB")
    return 0


def _command(args: argparse.Namespace) -> int:
    # A dry run takes the lock too: it opens a writable connection, may migrate and deletes before it rolls back.
    return run_exclusive(lambda: _run(args))


def register(db_sub: argparse._SubParsersAction) -> None:
    cmd = db_sub.add_parser(
        "prune",
        help="drop data no longer matching the domain filters (min_stars) and shrink cached READMEs",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cmd.add_argument("--apply", action="store_true", help="actually delete (default: dry run, nothing changes)")
    cmd.add_argument(
        "--snapshot-days",
        type=int,
        default=DEFAULT_SNAPSHOT_DAYS,
        metavar="N",
        help=f"keep every snapshot of the last N days, thin older ones to one per week (default {DEFAULT_SNAPSHOT_DAYS}; 0 = keep all)",
    )
    cmd.set_defaults(func=_command)
