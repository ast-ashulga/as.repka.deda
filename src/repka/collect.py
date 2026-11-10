"""Collect pipeline stage: search + enrich every due domain, with the scan bookkeeping.

Scan state, in one place:
- `domains.last_scan_at` only advances when a scan finished completely (status `ok`). A `partial` scan
  (rate limit, error, `max_repos_per_domain_scan` reached) records the status and leaves `last_scan_at`
  alone, so "last_scan_at is set" means "a complete baseline exists". An interrupted first scan therefore
  stays a silent baseline (RF8) until one scan completes.
- An unfinished scan keeps its window (stored with the cursor rows) until it completes: slice keys contain the
  window's dates, so a scan needing more than one UTC day could never finish if the window moved daily.
  A scan that finished on a stored window older than today records that window's end as `last_scan_at`, so
  the next window starts early enough to cover the days that passed meanwhile.
- `search_cursor` rows exist only for an unfinished scan: they let the next run skip finished slices (RF1)
  and are deleted when a scan completes, so a rerun on the same day really searches again.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from repka.config import AppConfig
from repka.domain.matrix import Query, Window, build_queries, scan_window
from repka.domain.schema import Domain, profile_hash
from repka.github.client import GitHubClient, GitHubError, RateLimited
from repka.github.enrich import EnrichStats, enrich_repos
from repka.github.search import QueryStats, collect_search
from repka.progress import NullReporter, Reporter
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


def _enrich_targets(
    conn: sqlite3.Connection, domain_id: str, now: datetime | None = None, refresh_every: timedelta = timedelta(0)
) -> tuple[list[int], set[int]]:
    """(repos to snapshot, accepted ids). Targets: repos matched after their latest snapshot (this scan's
    finds, and finds of an earlier interrupted run that never got enriched), repos never snapshotted, and
    accepted repos whose latest snapshot is at least `refresh_every` old (0 = all of them). `gone` repos
    are skipped."""
    targets: list[int] = []
    accepted: set[int] = set()
    for row in repo.repos_for_domain(conn, domain_id):
        if row["status"] == "gone":
            continue
        is_accepted = row["accepted"] == 1
        if is_accepted:
            accepted.add(row["repo_id"])
        latest = repo.latest_snapshot(conn, row["repo_id"])
        if latest is None or iso(latest.taken_at) < row["last_matched_at"]:
            targets.append(row["repo_id"])
        elif is_accepted and (now is None or now - latest.taken_at >= refresh_every):
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
    key_files_by_domain: dict[str, tuple[str, ...]],
    reporter: Reporter | None = None,
) -> DomainReport:
    state = repo.get_domain_state(conn, d.id)
    saved = repo.saved_window(conn, d.id)
    if saved:  # resuming an unfinished scan: same window, so its finished slices still match
        field_, dates = saved.split(":")
        start, end = (date.fromisoformat(x) for x in dates.split(".."))
        window = Window(field_, start, end)  # type: ignore[arg-type]
    else:
        window = scan_window(parse_iso(state.last_scan_at), now, cfg.collect)
        repo.save_window(conn, d.id, f"{window.field}:{window.start}..{window.end}", iso(now))
        conn.commit()
    emit_new = state.last_scan_at is not None  # first scan (no complete baseline yet) is silent
    queries = build_queries(d)
    say(
        f"{prefix} {d.id}: {'incremental' if emit_new else 'first (silent baseline)'} scan, "
        f"{len(queries)} queries, window {window.field}:{window.start}..{window.end}"
    )

    rep = reporter or NullReporter()
    rep.step(f"{d.id} search", "queries", len(queries))

    def on_query(index: int, count: int, query: Query, stats: QueryStats) -> None:
        search_total.add(stats)
        rep.advance(index, count)
        say(
            f"{prefix}   query {index}/{count} done: {stats.hits} repos ({stats.new} new), "
            f"{stats.slices} slices, {stats.pages} requests{f', {stats.capped} capped' if stats.capped else ''}"
        )

    limit = cfg.collect.max_repos_per_domain_scan
    search, _seen = collect_search(
        client, conn, d, window, iso(now), limit, emit_new, per_page=cfg.github.per_page, progress=on_query
    )
    targets, accepted = _enrich_targets(conn, d.id, now, cfg.collect.accepted_refresh_every)
    rep.step(f"{d.id} enrich", "repos", len(targets))
    say(f"{prefix}   enriching {len(targets)} repos ({len(accepted & set(targets))} accepted get slow metrics)")

    def on_enriched(done: int, total: int) -> None:
        say(f"{prefix}   enriched {done}/{total}")
        rep.advance(done, total)

    enrich = enrich_repos(
        client, conn, d.id, targets, d.key_files, now, cfg.collect, accepted, baseline=not emit_new,
        progress=on_enriched,
        key_files_by_domain=key_files_by_domain,
    )

    if search.hits >= limit:  # budget used up: some slices may be undone; the cursor lets the next run continue
        _record_partial(conn, d, state)
        return DomainReport(
            d.id,
            "partial",
            search,
            enrich,
            f"reached collect.max_repos_per_domain_scan ({limit}); run again to continue where it stopped",
        )
    scanned_at = iso(now) if window.end >= now.date() else iso(datetime.combine(window.end, time.min, UTC))
    # skipped repos have no snapshot, so the next run enriches them again; the scan itself is complete
    status = "partial" if enrich.skipped else "ok"
    repo.set_domain_scanned(conn, d.id, scanned_at, status, profile_hash(d), int(d.scan_every.total_seconds()))
    repo.clear_cursors(conn, d.id)
    conn.commit()
    error = f"{enrich.skipped} repos skipped: GraphQL kept failing for them, retried next run" if enrich.skipped else None
    return DomainReport(d.id, status, search, enrich, error)


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
    all_domains: list[Domain] | None = None,
    reporter: Reporter | None = None,
) -> CollectReport:
    """Search then enrich each domain (the caller decides which are due), using ONE `now` for the run.

    `RateLimited` marks the domain `partial`, the rest `skipped`, and returns normally (RF1: data kept).
    Any other `GitHubError` marks that domain `partial` (error recorded) and the next domain still runs.
    `AuthError` propagates untouched. `all_domains` (default: `domains`) tells enrichment which domains track which
    key files, since a repo can be shared with a domain that is not due.
    """
    say = progress or (lambda message: None)
    now = now.astimezone(UTC).replace(microsecond=0)
    report = CollectReport()
    key_files_by_domain = {d.id: tuple(d.key_files) for d in all_domains or domains}  # not just the due ones
    for index, d in enumerate(domains, 1):
        if report.rate_limited:
            report.domains.append(DomainReport(d.id, "skipped", error="skipped: GitHub rate limit reached earlier"))
            say(f"[{index}/{len(domains)}] {d.id}: skipped (rate limited)")
            continue
        search_so_far = QueryStats()  # a rate limit interrupts a query; keep what earlier queries found
        try:
            domain_report = _collect_one(client, conn, d, cfg, now, say, f"[{index}/{len(domains)}]", search_so_far, key_files_by_domain, reporter)
        except RateLimited as exc:
            _record_partial(conn, d, repo.get_domain_state(conn, d.id))
            report.rate_limited = True
            report.retry_at = exc.retry_at
            domain_report = DomainReport(d.id, "partial", search_so_far, error=_rate_limit_message(exc))
            log.warning("%s: %s", d.id, domain_report.error)
        except GitHubError as exc:  # this domain only: partial, the other domains still run (AuthError aborts)
            _record_partial(conn, d, repo.get_domain_state(conn, d.id))
            domain_report = DomainReport(d.id, "partial", search_so_far, error=f"GitHub error: {exc}")
            log.warning("%s: %s", d.id, domain_report.error)
        report.domains.append(domain_report)
    return report
