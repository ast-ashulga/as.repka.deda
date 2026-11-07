"""Enrichment: batched GraphQL snapshots, release / key-file / stack events, slow REST metrics.

GraphQL batches (`nodes(ids:)`) are keyed by node id and returned in request order, with `null` for
deleted or private repos. Per batch the network calls (main query, then one query for the text of
changed key files) happen before any write; repos are then written one by one and the batch is
committed even if a later repo raises `RateLimited`, so finished work is never lost.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from repka.config import CollectConfig, ConfigError
from repka.github.client import GitHubClient, GitHubError, RateLimited
from repka.github.stack import parse_deps
from repka.models import Event, KeyFile, Snapshot
from repka.store.repo import (
    add_event,
    insert_key_file,
    insert_snapshot,
    latest_key_file,
    latest_snapshot,
    mark_repo_gone,
    mark_slow_metrics,
    slow_metrics_fetched_at,
)

log = logging.getLogger(__name__)

NEW_RELEASE_MAX_AGE = timedelta(days=7)  # first sighting: only a release this fresh is an event
RELEASE_WINDOW = timedelta(days=90)

_NODE_FIELDS = """
      id
      databaseId
      stargazerCount
      forkCount
      issues(states: OPEN) { totalCount }
      watchers { totalCount }
      defaultBranchRef { target { ... on Commit {
        c7: history(since: $since7) { totalCount }
        c30: history(since: $since30) { totalCount }
      } } }
      releases(first: 10, orderBy: {field: CREATED_AT, direction: DESC}) {
        nodes { tagName name publishedAt createdAt url }
      }"""

_BLOB_TEXT_QUERY = "query($ids: [ID!]!) { nodes(ids: $ids) { ... on Blob { text } } }"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class EnrichStats:
    repos: int = 0
    gone: int = 0
    events: int = 0
    slow_refreshed: int = 0


def build_enrich_query(key_files: Sequence[str]) -> tuple[str, dict[str, str]]:
    """GraphQL text for one batch plus the alias -> key-file path map (`kf0..kfN`).

    Deviation from the plan text: `$since90` is not declared (an unused variable is a GraphQL
    validation error); `releases_90d` is counted client-side from the last 10 releases.
    """
    aliases = {f"kf{i}": path for i, path in enumerate(key_files)}
    blobs = "".join(
        f"\n      {alias}: object(expression: {json.dumps('HEAD:' + path)}) {{ ... on Blob {{ id oid }} }}"
        for alias, path in aliases.items()
    )
    text = (
        "query($ids: [ID!]!, $since7: GitTimestamp!, $since30: GitTimestamp!) {\n"
        f"  nodes(ids: $ids) {{\n    ... on Repository {{{_NODE_FIELDS}{blobs}\n    }}\n  }}\n}}"
    )
    return text, aliases


def snapshot_from_node(node: dict, now: datetime, prev: Snapshot | None) -> Snapshot:
    target = (node.get("defaultBranchRef") or {}).get("target") or {}  # null for an empty repo
    releases = (node.get("releases") or {}).get("nodes") or []
    cutoff = now - RELEASE_WINDOW
    latest = releases[0] if releases else None
    return Snapshot(
        repo_id=node["databaseId"],
        taken_at=now,
        stars=node["stargazerCount"],
        forks=node["forkCount"],
        open_issues=node["issues"]["totalCount"],
        watchers=node["watchers"]["totalCount"],
        contributors=prev.contributors if prev else None,
        commits_7d=(target.get("c7") or {}).get("totalCount", 0),
        commits_30d=(target.get("c30") or {}).get("totalCount", 0),
        active_weeks_12=prev.active_weeks_12 if prev else None,
        releases_90d=sum(1 for r in releases if _ts(r["createdAt"]) >= cutoff),  # capped at 10 releases
        latest_release_tag=latest["tagName"] if latest else None,
        latest_release_at=_ts(latest.get("publishedAt") or latest["createdAt"]) if latest else None,
    )


def _due(fetched_at: str | None, now: datetime, every: timedelta) -> bool:
    return fetched_at is None or _ts(fetched_at) <= now - every


def _fetch_slow(call, what: str, full_name: str) -> Any:
    try:
        return call(full_name)
    except RateLimited:
        raise
    except GitHubError as exc:  # e.g. 404 on a repo that vanished meanwhile: leave NULL, retry next run
        log.warning("%s of %s failed: %s", what, full_name, exc)
        return None


def _refresh_slow(
    client: GitHubClient,
    conn: sqlite3.Connection,
    snap: Snapshot,
    full_name: str,
    now: datetime,
    cfg: CollectConfig,
) -> tuple[Snapshot, str | None, str | None]:
    """Fetch whichever slow metric is due. Returns (snapshot, contributors_at, participation_at) stamps."""
    contributors_at, participation_at = slow_metrics_fetched_at(conn, snap.repo_id)
    new_c = new_p = None
    if _due(contributors_at, now, cfg.slow_metrics_every):
        value = _fetch_slow(client.contributors_count, "contributors", full_name)
        snap = replace(snap, contributors=value)  # None is stored as NULL and retried next run
        new_c = _iso(now) if value is not None else None
    if _due(participation_at, now, cfg.slow_metrics_every):
        series = _fetch_slow(client.participation, "participation", full_name)
        weeks = sum(1 for w in series[-12:] if w) if series is not None else None
        snap = replace(snap, active_weeks_12=weeks)
        new_p = _iso(now) if weeks is not None else None
    return snap, new_c, new_p


def _changed_blobs(
    conn: sqlite3.Connection, batch: Sequence[int], nodes: Sequence[dict | None], aliases: dict[str, str]
) -> list[str]:
    """Blob node ids of every key file whose oid differs from the stored one."""
    wanted = []
    for repo_id, node in zip(batch, nodes):
        for alias, path in aliases.items():
            blob = (node or {}).get(alias)
            known = latest_key_file(conn, repo_id, path)
            if blob and (known is None or known[0] != blob["oid"]):
                wanted.append(blob["id"])
    return wanted


def _fetch_texts(client: GitHubClient, blob_ids: list[str]) -> dict[str, str | None]:
    if not blob_ids:
        return {}
    data = client.graphql(_BLOB_TEXT_QUERY, {"ids": blob_ids})
    texts = data.get("nodes") or []
    if len(texts) != len(blob_ids):
        raise GitHubError(f"GraphQL returned {len(texts)} blobs for {len(blob_ids)} ids")
    return {bid: (t or {}).get("text") for bid, t in zip(blob_ids, texts)}  # text is null for binary/huge files


def _release_event(
    repo_id: int,
    domain_id: str,
    snap: Snapshot,
    prev: Snapshot | None,
    node: dict,
    now: datetime,
    baseline: bool = False,
) -> Event | None:
    tag = snap.latest_release_tag
    if tag is None:
        return None
    if baseline and prev is None:  # silent baseline scan: a first sighting is not news (P1-T22)
        return None
    if prev is not None:
        if prev.latest_release_tag == tag:
            return None
    elif snap.latest_release_at is None or snap.latest_release_at < now - NEW_RELEASE_MAX_AGE:
        return None
    latest = node["releases"]["nodes"][0]
    payload = {
        "tag": tag,
        "name": latest.get("name"),
        "published_at": latest.get("publishedAt") or latest["createdAt"],
        "url": latest.get("url"),
    }
    return Event(repo_id, domain_id, "release", payload, now)


def _key_file_events(
    conn: sqlite3.Connection,
    repo_id: int,
    domain_id: str,
    node: dict,
    aliases: dict[str, str],
    texts: dict[str, str | None],
    now: datetime,
) -> list[Event]:
    events: list[Event] = []
    for alias, path in aliases.items():
        blob = node.get(alias)
        if not blob:  # file missing: nothing stored
            continue
        known = latest_key_file(conn, repo_id, path)
        if known is not None and known[0] == blob["oid"]:
            continue
        text = texts.get(blob["id"])
        deps = parse_deps(path, text) if text is not None else None
        insert_key_file(conn, KeyFile(repo_id, path, blob["oid"], text), deps, _iso(now))
        if known is None:  # first sighting: baseline, no events
            continue
        old_oid, old_deps = known
        changed = {"path": path, "old_oid": old_oid, "new_oid": blob["oid"]}
        events.append(Event(repo_id, domain_id, "key_file_changed", changed, now))
        if deps is not None and old_deps is not None and deps != old_deps:
            payload = {
                "path": path,
                "added": sorted(set(deps) - set(old_deps)),
                "removed": sorted(set(old_deps) - set(deps)),
            }
            events.append(Event(repo_id, domain_id, "stack_changed", payload, now))
    return events


def _enrich_one(
    client: GitHubClient,
    conn: sqlite3.Connection,
    domain_id: str,
    repo_id: int,
    node: dict | None,
    full_name: str,
    aliases: dict[str, str],
    texts: dict[str, str | None],
    now: datetime,
    cfg: CollectConfig,
    slow_for: set[int],
    stats: EnrichStats,
    baseline: bool = False,
) -> None:
    if not node:  # deleted / private / not a repository: keep history
        mark_repo_gone(conn, repo_id)
        stats.gone += 1
        return
    prev = latest_snapshot(conn, repo_id)
    if prev is not None and _iso(prev.taken_at) == _iso(now):
        return  # already enriched at this instant (same repo in two domains of one run)
    snap = snapshot_from_node(node, now, prev)
    stamps = (None, None)
    if repo_id in slow_for:
        snap, *stamps = _refresh_slow(client, conn, snap, full_name, now, cfg)
    insert_snapshot(conn, snap)
    mark_slow_metrics(conn, repo_id, *stamps)
    events = []
    release = _release_event(repo_id, domain_id, snap, prev, node, now, baseline)
    if release:
        events.append(release)
    events += _key_file_events(conn, repo_id, domain_id, node, aliases, texts, now)
    for event in events:
        add_event(conn, event)
    stats.slow_refreshed += any(stamps)
    stats.events += len(events)
    stats.repos += 1


def enrich_repos(
    client: GitHubClient,
    conn: sqlite3.Connection,
    domain_id: str,
    repo_ids: Sequence[int],
    key_files: Sequence[str],
    now: datetime,
    cfg: CollectConfig,
    slow_for: set[int],
    baseline: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> EnrichStats:
    """Snapshot `repo_ids` in batches of `cfg.enrich_batch`; slow REST metrics only for `slow_for`.

    `baseline=True` (a domain's silent first scan) emits no `release` event for a repo seen for the first time,
    so releases up to a week old do not flood the first digest; a repo that already has a snapshot still can."""
    stats = EnrichStats()
    query, aliases = build_enrich_query(key_files)
    since = {"since7": _iso(now - timedelta(days=7)), "since30": _iso(now - timedelta(days=30))}
    rows = {}
    for repo_id in repo_ids:
        row = conn.execute("SELECT node_id, full_name FROM repos WHERE repo_id = ?", (repo_id,)).fetchone()
        if row is None:
            log.warning("enrich: repo %s is not in the store, skipped", repo_id)
        else:
            rows[repo_id] = row
    ids = list(rows)

    for start in range(0, len(ids), cfg.enrich_batch):
        batch = ids[start : start + cfg.enrich_batch]
        data = client.graphql(query, {"ids": [rows[r]["node_id"] for r in batch], **since})
        nodes = data.get("nodes") or []
        if len(nodes) != len(batch):
            raise GitHubError(f"GraphQL returned {len(nodes)} nodes for {len(batch)} ids")
        texts = _fetch_texts(client, _changed_blobs(conn, batch, nodes, aliases))
        try:
            for repo_id, node in zip(batch, nodes):
                conn.execute("SAVEPOINT enrich_repo")
                try:
                    _enrich_one(client, conn, domain_id, repo_id, node, rows[repo_id]["full_name"],
                                aliases, texts, now, cfg, slow_for, stats, baseline)
                except (RateLimited, ConfigError):  # AuthError is a ConfigError: both must stop the run
                    conn.execute("ROLLBACK TO enrich_repo")
                    raise
                except Exception:
                    log.exception("enrich: repo %s skipped after an unexpected error", repo_id)
                    conn.execute("ROLLBACK TO enrich_repo")
                finally:
                    conn.execute("RELEASE enrich_repo")
        finally:
            conn.commit()  # keep finished repos even if RateLimited interrupts the batch
        if progress is not None:
            progress(min(start + cfg.enrich_batch, len(ids)), len(ids))
    return stats
