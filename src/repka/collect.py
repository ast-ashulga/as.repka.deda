"""Collect pipeline stage: search + enrich every due domain, with the scan bookkeeping.

Scan state, in one place:
- `domains.last_scan_at` only advances when a scan finished completely (status `ok`). A `partial` scan
  (rate limit, error, `max_repos_per_domain_scan` reached) records the status and leaves `last_scan_at`
  alone, so "last_scan_at is set" means "a complete baseline exists". An interrupted first scan therefore
  stays a silent baseline (RF8) until one scan completes.
- `search_cursor` rows exist only for an unfinished scan: they let the next run skip finished slices (RF1)
  and are deleted when a scan completes, so a rerun on the same day really searches again.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from repka.config import AppConfig
from repka.domain.matrix import Query, build_queries, scan_window
from repka.domain.schema import Domain, profile_hash
from repka.github.client import GitHubClient, GitHubError, RateLimited
from repka.github.enrich import EnrichStats, enrich_repos
from repka.github.search import QueryStats, collect_search
from repka.store import repo
from repka.store.repo import DomainState

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


@dataclass
class DomainReport:
    domain_id: str
    status: Literal["ok", "partial", "skipped"]
    search: QueryStats = field(default_factory=QueryStats)
    enrich: EnrichStats = field(default_factory=EnrichStats)
    error: str | None = None


@dataclass
class CollectReport:
    domains: list[DomainReport] = field(default_factory=list)
    rate_limited: bool = False
    retry_at: float | None = None  # wall-clock epoch (time.time scale) from GitHub, when it said


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def next_due(d: Domain, state: DomainState | None) -> datetime | None:
    """When the domain is due again; None if it is due now (never scanned, or its last scan was partial)."""
    last = parse_iso(state.last_scan_at) if state else None
    if last is None or state.last_scan_status == "partial":
        return None
    return last + d.scan_every


def due_domains(domains: list[Domain], states: dict[str, DomainState], now: datetime) -> list[Domain]:
    """Enabled domains that were never scanned, whose last scan was partial (resume), or whose interval passed."""
    due = []
    for d in domains:
        if not d.enabled:
            continue
        at = next_due(d, states.get(d.id))
        if at is None or now >= at:
            due.append(d)
    return due


def _enrich_targets(conn: sqlite3.Connection, domain_id: str, since: str | None) -> tuple[list[int], set[int]]:
    """(repos to snapshot, accepted ids). Targets: everything matched since the last complete scan
    (includes slices stored by an earlier partial run), accepted repos, and repos never snapshotted."""
    targets: list[int] = []
    accepted: set[int] = set()
    for row in repo.repos_for_domain(conn, domain_id):
        if row["status"] == "gone":
            continue
        is_accepted = row["accepted"] == 1
        if is_accepted:
            accepted.add(row["repo_id"])
        if (
            is_accepted
            or since is None
            or row["last_matched_at"] > since
            or repo.latest_snapshot(conn, row["repo_id"]) is None
        ):
            targets.append(row["repo_id"])
    return targets, accepted


def _record_partial(conn: sqlite3.Connection, d: Domain, state: DomainState) -> None:
    # last_scan_at stays what it was (None after an interrupted first scan): see the module docstring.
    repo.set_domain_scanned(
        conn, d.id, state.last_scan_at, "partial", profile_hash(d), int(d.scan_every.total_seconds())
    )
    conn.commit()


def _collect_one(
    client: GitHubClient,
    conn: sqlite3.Connection,
    d: Domain,
    cfg: AppConfig,
    now: datetime,
    say: Progress,
    prefix: str,
    search_total: QueryStats,
) -> DomainReport:
    state = repo.get_domain_state(conn, d.id)
    window = scan_window(parse_iso(state.last_scan_at), now, cfg.collect)
    emit_new = state.last_scan_at is not None  # first scan (no complete baseline yet) is silent
    queries = build_queries(d)
    say(
        f"{prefix} {d.id}: {'incremental' if emit_new else 'first (silent baseline)'} scan, "
        f"{len(queries)} queries, window {window.field}:{window.start}..{window.end}"
    )

    def on_query(index: int, count: int, query: Query, stats: QueryStats) -> None:
        search_total.add(stats)
        say(
            f"{prefix}   query {index}/{count} done: {stats.hits} repos ({stats.new} new), "
            f"{stats.slices} slices, {stats.pages} requests{f', {stats.capped} capped' if stats.capped else ''}"
        )

    limit = cfg.collect.max_repos_per_domain_scan
    search, _seen = collect_search(
        client, conn, d, window, iso(now), limit, emit_new, per_page=cfg.github.per_page, progress=on_query
    )
    targets, accepted = _enrich_targets(conn, d.id, state.last_scan_at)
    say(f"{prefix}   enriching {len(targets)} repos ({len(accepted & set(targets))} accepted get slow metrics)")
    enrich = enrich_repos(client, conn, d.id, targets, d.key_files, now, cfg.collect, accepted)

    if search.hits >= limit:  # budget used up: some slices may be undone; the cursor lets the next run continue
        _record_partial(conn, d, state)
        return DomainReport(
            d.id,
            "partial",
            search,
            enrich,
            f"reached collect.max_repos_per_domain_scan ({limit}); run again to continue where it stopped",
        )
    repo.set_domain_scanned(conn, d.id, iso(now), "ok", profile_hash(d), int(d.scan_every.total_seconds()))
    repo.clear_cursors(conn, d.id)
    conn.commit()
    return DomainReport(d.id, "ok", search, enrich)


def _rate_limit_message(exc: RateLimited) -> str:
    message = f"GitHub rate limit: {exc}"
    if exc.retry_at is not None:
        when = datetime.fromtimestamp(exc.retry_at).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        message += f"; retry after {when}"
    return message


def collect_domains(
    client: GitHubClient,
    conn: sqlite3.Connection,
    domains: list[Domain],
    cfg: AppConfig,
    now: datetime,
    *,
    progress: Progress | None = None,
) -> CollectReport:
    """Search then enrich each domain (the caller decides which are due), using ONE `now` for the run.

    `RateLimited` marks the domain `partial`, the rest `skipped`, and returns normally (RF1: data kept).
    Any other `GitHubError` marks the domain `partial` and propagates. `AuthError` propagates untouched.
    """
    say = progress or (lambda message: None)
    now = now.astimezone(UTC).replace(microsecond=0)
    report = CollectReport()
    for index, d in enumerate(domains, 1):
        if report.rate_limited:
            report.domains.append(DomainReport(d.id, "skipped", error="skipped: GitHub rate limit reached earlier"))
            say(f"[{index}/{len(domains)}] {d.id}: skipped (rate limited)")
            continue
        search_so_far = QueryStats()  # a rate limit interrupts a query; keep what earlier queries found
        try:
            domain_report = _collect_one(client, conn, d, cfg, now, say, f"[{index}/{len(domains)}]", search_so_far)
        except RateLimited as exc:
            _record_partial(conn, d, repo.get_domain_state(conn, d.id))
            report.rate_limited = True
            report.retry_at = exc.retry_at
            domain_report = DomainReport(d.id, "partial", search_so_far, error=_rate_limit_message(exc))
            log.warning("%s: %s", d.id, domain_report.error)
        except GitHubError:
            _record_partial(conn, d, repo.get_domain_state(conn, d.id))
            raise
        report.domains.append(domain_report)
    return report
