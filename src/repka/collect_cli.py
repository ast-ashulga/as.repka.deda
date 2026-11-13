"""`repka collect`: search + enrich the due domains (or one) and print a summary."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime

from repka.cliutil import stderr_progress
from repka.collect import (
    CollectReport,
    DomainReport,
    collect_domains,
    due_domains,
    iso,
    next_due,
    parse_iso,
)
from repka.config import (
    AppConfig,
    ConfigError,
    db_path,
    domains_dir,
    github_token,
    github_token_source,
    load_config,
)
from repka.domain.matrix import build_queries, scan_window
from repka.domain.schema import Domain, get_domain, load_domains
from repka.github.client import GitHubClient, GitHubError
from repka.runlock import run_exclusive
from repka.store import repo
from repka.store.db import connect, migrate
from repka.store.repo import DomainState, SqliteEtagCache
from repka.textutil import printable


def make_client(cfg: AppConfig, conn: sqlite3.Connection) -> GitHubClient:
    """The real GitHub client. Resolves the token here (ConfigError, exit 2, when there is none)."""
    return GitHubClient(
        github_token(), cfg.github, cache=SqliteEtagCache(conn), token_source=github_token_source()
    )


def _states(domains: list[Domain], conn: sqlite3.Connection | None) -> dict[str, DomainState]:
    if conn is None:
        return {}
    return {d.id: repo.get_domain_state(conn, d.id) for d in domains}


def _select(args: argparse.Namespace) -> tuple[list[Domain], bool]:
    """(candidate domains, forced). `--domain` forces that one even if it is not due."""
    if args.domain:
        d = get_domain(args.domain)
        if not d.enabled:
            raise ConfigError(
                f"domain {d.id!r} is disabled: set `enabled: true` in {domains_dir() / (d.id + '.yaml')} to collect it"
            )
        return [d], True
    return [d for d in load_domains() if d.enabled], False


def _not_due(candidates: list[Domain], due: list[Domain], states: dict[str, DomainState]) -> list[dict]:
    due_ids = {d.id for d in due}
    rows = []
    for d in candidates:
        if d.id in due_ids:
            continue
        state = states.get(d.id)
        at = next_due(d, state)
        rows.append({"domain": d.id, "last_scan_at": state.last_scan_at if state else None, "next_due_at": iso(at) if at else None})
    return rows


def _not_due_text(rows: list[dict]) -> list[str]:
    return [f"not due: {r['domain']} (scanned {r['last_scan_at']}, next scan after {r['next_due_at']})" for r in rows]


def _dry_run(due: list[Domain], states: dict[str, DomainState], cfg: AppConfig, now: datetime, not_due: list[dict], as_json: bool) -> int:
    plans = []
    for d in due:
        last = parse_iso(states[d.id].last_scan_at) if d.id in states else None
        window = scan_window(last, now, cfg.collect)
        plans.append(
            {
                "domain": d.id,
                "scan": "incremental" if last else "first",
                "queries": len(build_queries(d)),
                "window": {"field": window.field, "start": window.start.isoformat(), "end": window.end.isoformat()},
            }
        )
    if as_json:
        print(json.dumps({"dry_run": True, "domains": plans, "not_due": not_due}, ensure_ascii=False, indent=2))
        return 0
    for p in plans:
        w = p["window"]
        print(f"[dry-run] {p['domain']}: {p['scan']} scan, {p['queries']} queries, window {w['field']}:{w['start']}..{w['end']}")
    if not plans:
        print("[dry-run] nothing is due")
    for line in _not_due_text(not_due):
        print(line)
    return 0


def domain_line(r: DomainReport) -> str:
    if r.status == "skipped":
        return f"{r.domain_id}  skipped"
    return (
        f"{r.domain_id}  {r.status}  found {r.search.hits} repos ({r.search.new} new) in {r.search.slices} slices, "
        f"snapshotted {r.enrich.repos} ({r.enrich.gone} gone), {r.enrich.events} events"
    )


def _summary(report: CollectReport, not_due: list[dict], seconds: float, as_json: bool) -> None:
    status = "ok" if all(r.status == "ok" for r in report.domains) else "partial"
    if as_json:
        payload = {
            "status": status,
            "rate_limited": report.rate_limited,
            "retry_at": datetime.fromtimestamp(report.retry_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if report.retry_at else None,
            "duration_s": round(seconds, 1),
            "domains": [
                {**dataclasses.asdict(r), "error": printable(r.error) if r.error else None} for r in report.domains
            ],
            "not_due": not_due,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for r in report.domains:
        print(domain_line(r))
        if r.error:
            print(f"  note: {printable(r.error)}")
    if report.rate_limited:
        print("rate limited: data collected so far is kept; run `repka collect` again later to continue")
    for line in _not_due_text(not_due):
        print(line)
    print(f"{status}: {len(report.domains)} domain(s) in {seconds:.0f}s")


def _collect(args: argparse.Namespace) -> int:
    cfg = load_config()
    now = datetime.now(UTC).replace(microsecond=0)
    candidates, forced = _select(args)

    if args.dry_run:  # read-only: no migration, no lock, no client
        conn = connect(readonly=True) if db_path().exists() else None
        try:
            states = _states(candidates, conn)
        except sqlite3.OperationalError:  # database exists but is not migrated yet
            states = {}
        finally:
            if conn is not None:
                conn.close()
        due = candidates if forced else due_domains(candidates, states, now)
        return _dry_run(due, states, cfg, now, _not_due(candidates, due, states), args.json)

    return run_exclusive(lambda: _collect_locked(args, cfg, now, candidates, forced))


def _collect_locked(args: argparse.Namespace, cfg: AppConfig, now: datetime, candidates: list[Domain], forced: bool) -> int:
    conn = connect()
    client = None
    try:
        migrate(conn)
        states = _states(candidates, conn)
        due = candidates if forced else due_domains(candidates, states, now)
        not_due = _not_due(candidates, due, states)
        if not due:
            if not candidates:
                print(f"no enabled domains in {domains_dir()}: set `enabled: true` in a domain file", file=sys.stderr)
            if args.json:
                print(json.dumps({"status": "ok", "rate_limited": False, "domains": [], "not_due": not_due}, indent=2))
            else:
                print("nothing due" + ("" if not not_due else ": use `--domain ID` to force one"))
                for line in _not_due_text(not_due):
                    print(line)
            return 0

        client = make_client(cfg, conn)  # after the due check: nothing due needs no token
        run_id = repo.start_run(conn, "collect")
        conn.commit()
        started = time.monotonic()
        try:
            report = collect_domains(client, conn, due, cfg, now, progress=stderr_progress, all_domains=candidates)
        except Exception as exc:
            repo.finish_run(conn, run_id, "failed", {}, str(exc))
            conn.commit()
            if isinstance(exc, GitHubError):
                print(f"error: GitHub request failed: {printable(str(exc))} (progress is kept; run again)", file=sys.stderr)
                return 1
            raise
        status = "ok" if all(r.status == "ok" for r in report.domains) else "partial"
        errors = "; ".join(f"{r.domain_id}: {r.error}" for r in report.domains if r.error) or None
        repo.finish_run(conn, run_id, status, {"domains": [dataclasses.asdict(r) for r in report.domains]}, errors)
        conn.commit()
        _summary(report, not_due, time.monotonic() - started, args.json)
        return 1 if any(r.github_error for r in report.domains) else 0  # a rate limit is a clean stop (0)
    finally:
        if client is not None:
            client.close()
        conn.close()


def register(sub: argparse._SubParsersAction) -> None:
    collect = sub.add_parser(
        "collect",
        help="search GitHub and snapshot repos for the due domains",
        description=(
            "Search every due, enabled domain (or only --domain ID, even if not due), then enrich the found repos "
            "with metrics, releases and key files. The first scan of a domain is a silent baseline (no new-repo "
            "events). Progress goes to stderr, the summary to stdout. A GitHub rate limit stops the run cleanly: "
            "everything collected is kept and the next run continues (exit 0, status partial). Any other GitHub error "
            "stops that domain (the others still run), progress is kept and the exit code is 1."
        ),
    )
    collect.add_argument("--domain", metavar="ID", help="collect only this domain, even if it is not due")
    collect.add_argument("--dry-run", action="store_true", help="print each domain's window and query count; no network, no writes")
    collect.add_argument("--json", action="store_true", help="print the summary as JSON on stdout")
    collect.set_defaults(func=_collect)
