"""Data layer for the dashboard: pages call these functions and hold no SQL.

The query functions take an open connection first (the app passes `store.db.connect(readonly=True)`;
tests pass a fixture DB) and mostly return a pandas DataFrame; a few helpers return plain values
(`gap_after`, `sparkline_values`) or a tuple (`landscape_frame`). They only read, except
`store_landscape`, which upserts the Landscape cache and needs a writable, migrated connection (the
dashboard's one database write; its other write is the domain YAML files, done by the Domains page).
Metric values come from `store.metrics`. Text that other people wrote (names, descriptions, error
messages, release tags) goes through `textutil.printable()` here before it reaches a table.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from repka.config import DigestConfig
from repka.digest.build import pending_overflow
from repka.digest.releases import build_releases
from repka.digest.slots import slot_key
from repka.domain.schema import Domain, format_duration
from repka.releases_cli import releases_since
from repka.store import repo
from repka.store.metrics import repo_metrics, stars_delta, velocity
from repka.textutil import printable


def _text(value: Any) -> Any:
    return printable(value) if isinstance(value, str) else value


def shown(frame: pd.DataFrame) -> pd.DataFrame:
    """Display shaping only: null cells read as n/a instead of 'None'."""
    return frame.fillna("n/a")


def domains_overview(conn: sqlite3.Connection, domains: Sequence[Domain]) -> pd.DataFrame:
    """One row per domain definition: schedule, last scan and repo counts (accepted = latest verdict accepted)."""
    rows = []
    for d in domains:
        state = repo.get_domain_state(conn, d.id)
        repos = repo.repos_for_domain(conn, d.id)
        rows.append(
            {
                "id": d.id,
                "name": _text(d.name),
                "enabled": d.enabled,
                "scan_every": format_duration(d.scan_every),
                "last_scan_at": state.last_scan_at,
                "last_scan_status": state.last_scan_status,
                "repos": len(repos),
                "accepted": sum(1 for r in repos if r["accepted"] == 1),
            }
        )
    return pd.DataFrame(rows, columns=_DOMAIN_COLUMNS)


_DOMAIN_COLUMNS = [
    "id", "name", "enabled", "scan_every", "last_scan_at", "last_scan_status", "repos", "accepted",
]


def repos_frame(
    conn: sqlite3.Connection, domain_id: str, accepted_only: bool = False, now: datetime | None = None
) -> pd.DataFrame:
    """Repos of a domain with their latest metrics (stars, 7-day star delta, velocity); n/a is NaN/None."""
    now = now or datetime.now(UTC)
    rows = []
    for r in repo.repos_for_domain(conn, domain_id, accepted_only=accepted_only):
        metrics = repo_metrics(conn, r["repo_id"], now)
        rows.append(
            {
                "repo_id": r["repo_id"],
                "full_name": _text(r["full_name"]),
                "html_url": r["html_url"],
                "description": _text(r["description"]),
                "language": _text(r["language"]),
                "is_fork": bool(r["is_fork"]),
                "accepted": None if r["accepted"] is None else bool(r["accepted"]),
                "stars": metrics.stars if metrics else None,
                "stars_delta_7d": metrics.stars_delta_7d if metrics else None,
                "velocity": metrics.velocity if metrics else None,
                "first_matched_at": r["first_matched_at"],
                "pushed_at": r["pushed_at"],
            }
        )
    frame = pd.DataFrame(rows, columns=_REPO_COLUMNS)
    return frame.sort_values("stars", ascending=False, na_position="last", kind="stable", ignore_index=True)


_REPO_COLUMNS = [
    "repo_id", "full_name", "html_url", "description", "language", "is_fork", "accepted",
    "stars", "stars_delta_7d", "velocity", "first_matched_at", "pushed_at",
]


def metrics_history(conn: sqlite3.Connection, repo_ids: Iterable[int]) -> pd.DataFrame:
    """One row per snapshot of each repo. Deltas and velocity are the values `store.metrics` gives at that
    snapshot's time; gaps between snapshots are not filled in."""
    rows = []
    for repo_id in sorted(set(repo_ids)):
        name_row = conn.execute("SELECT full_name FROM repos WHERE repo_id = ?", (repo_id,)).fetchone()
        full_name = _text(name_row["full_name"]) if name_row else None
        snaps = repo.snapshots_for(conn, repo_id)
        for s in snaps:
            rows.append(
                {
                    "repo_id": repo_id,
                    "full_name": full_name,
                    "taken_at": s.taken_at,
                    "stars": s.stars,
                    "forks": s.forks,
                    "commits_30d": s.commits_30d,
                    "releases_90d": s.releases_90d,
                    "stars_delta_7d": stars_delta(snaps, s.taken_at, 7),
                    "velocity": velocity(s),
                }
            )
    frame = pd.DataFrame(rows, columns=_HISTORY_COLUMNS)
    frame["taken_at"] = pd.to_datetime(frame["taken_at"], utc=True)
    return frame


_HISTORY_COLUMNS = [
    "repo_id", "full_name", "taken_at", "stars", "forks", "commits_30d", "releases_90d",
    "stars_delta_7d", "velocity",
]


def stack_history(conn: sqlite3.Connection, repo_id: int) -> pd.DataFrame:
    """Dependency changes of one repo, per key file, in the order the file versions were seen.

    The first version of a file lists every dependency as added; later versions list the diff.
    Versions whose dependency list was not fetched (deps_json NULL) are skipped.
    """
    rows = conn.execute(
        "SELECT path, blob_oid, deps_json, first_seen_at FROM key_files WHERE repo_id = ? "
        "ORDER BY path, first_seen_at",
        (repo_id,),
    ).fetchall()
    out = []
    previous: dict[str, set[str]] = {}
    for row in rows:
        if row["deps_json"] is None:
            continue
        deps = set(json.loads(row["deps_json"]))
        before = previous.get(row["path"], set())
        for change, names in (("added", deps - before), ("removed", before - deps)):
            for dep in sorted(names):
                out.append(
                    {
                        "path": _text(row["path"]),
                        "blob_oid": row["blob_oid"],
                        "first_seen_at": row["first_seen_at"],
                        "change": change,
                        "dependency": _text(dep),
                    }
                )
        previous[row["path"]] = deps
    return pd.DataFrame(out, columns=_STACK_COLUMNS)


_STACK_COLUMNS = ["path", "blob_oid", "first_seen_at", "change", "dependency"]


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT id, kind, started_at, finished_at, status, error FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    frame = pd.DataFrame([dict(r) for r in rows], columns=_RUN_COLUMNS)
    frame["error"] = frame["error"].map(_text)
    return frame


_RUN_COLUMNS = ["id", "kind", "started_at", "finished_at", "status", "error"]


def recent_digests(conn: sqlite3.Connection, limit: int = 10) -> pd.DataFrame:
    """Newest first. `kind` is main or releases (the daily releases message); `slot` is the plain HH:MM."""
    rows = conn.execute(
        "SELECT id, kind, slot, local_date, status, item_count, sent_at FROM digests ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    frame = pd.DataFrame([dict(r) for r in rows], columns=_DIGEST_COLUMNS)
    frame["slot"] = [slot.removeprefix(slot_key(kind, "")) for kind, slot in zip(frame["kind"], frame["slot"], strict=True)]
    return frame


_DIGEST_COLUMNS = ["id", "kind", "slot", "local_date", "status", "item_count", "sent_at"]

_RELEASE_COLUMNS = ["domain", "repo", "stars", "tag", "published_at"]
_WAITING_COLUMNS = ["domain", "new", "found", "release", "total"]


def recent_releases(
    conn: sqlite3.Connection, domains: Sequence[Domain], since: timedelta, now: datetime | None = None
) -> pd.DataFrame:
    """Latest release event per accepted repo in the window (sent and pending alike), most stars first per domain.

    Same rows as `repka releases --since`: it calls the same query, so the page and the command agree.
    """
    cutoff = (now or datetime.now(UTC)) - since
    rows = [
        {"domain": _text(d.name), "repo": _text(r.full_name), "stars": r.stars, "tag": _text(r.tag), "published_at": r.published_at}
        for d in domains
        for r in releases_since(conn, d, cutoff)
    ]
    return pd.DataFrame(rows, columns=_RELEASE_COLUMNS)


def waiting_items(
    conn: sqlite3.Connection, domains: Sequence[Domain], cfg: DigestConfig, now: datetime | None = None
) -> pd.DataFrame:
    """What the next messages hold: new / found items of the main digest and pending releases, per domain.

    New and found are the `repka digest --more` queue (stack changes and movers never wait). With
    `digest.releases_message` on, releases belong to the releases message, so that column counts what it
    would send; otherwise it counts the releases waiting in the main digest.
    """
    now = now or datetime.now(UTC)
    today = now.astimezone().date().isoformat()
    main = pending_overflow(conn, list(domains), now, cfg, "00:00", today)
    counts: dict[str, Counter[str]] = {}
    names: dict[str, str] = {}
    for s in main.sections:
        names[s.domain_id] = s.domain_name
        counts[s.domain_id] = Counter(i.line_kind for i in (*s.new_repos, *s.releases))
    if cfg.releases_message:
        for rs in build_releases(conn, list(domains), now, cfg, "00:00", today).sections:
            names[rs.domain_id] = rs.domain_name
            counts.setdefault(rs.domain_id, Counter())["release"] = len(rs.items) + rs.more
    rows = [
        {
            "domain": _text(names[d]),
            "new": c["new"],
            "found": c["found"],
            "release": c["release"],
            "total": c["new"] + c["found"] + c["release"],
        }
        for d, c in counts.items()
    ]
    return pd.DataFrame(rows, columns=_WAITING_COLUMNS)


# --- Landscape map ----------------------------------------------------------------------------------
# The UMAP result is cached in `landscape_cache` (migration 003). Reading the cache and computing the map
# only read; `store_landscape` is the dashboard's one database write (besides the domain YAML files).

LANDSCAPE_METHOD = "umap"
LANDSCAPE_SEED = 42
LANDSCAPE_MIN_CLUSTER = 5


def _landscape_inputs(conn: sqlite3.Connection, domain_id: str, model: str):
    """Repo ids and the L2-normalized matrix of the domain's repos for ONE embedding model."""
    from repka.store.vectors import load_matrix

    return load_matrix(conn, model, [r["repo_id"] for r in repo.repos_for_domain(conn, domain_id)])


def compute_landscape(
    conn: sqlite3.Connection,
    domain_id: str,
    model: str,
    method: str = LANDSCAPE_METHOD,
    seed: int = LANDSCAPE_SEED,
    min_cluster_size: int = LANDSCAPE_MIN_CLUSTER,
) -> dict[str, Any] | None:
    """Project and cluster the domain's vectors; returns the cache row, or None without embeddings."""
    from repka import landscape

    ids, matrix = _landscape_inputs(conn, domain_id, model)
    if not ids:
        return None
    points = landscape.project(matrix, method, seed)
    labels = landscape.cluster(points, min_cluster_size)
    by_id = {r["repo_id"]: r for r in repo.repos_for_domain(conn, domain_id)}
    labelled = pd.DataFrame(
        {
            "cluster": labels,
            "topics": [json.loads(by_id[i]["topics_json"] or "[]") for i in ids],
            "description": [by_id[i]["description"] for i in ids],
        }
    )
    payload = {
        "repo_ids": ids,
        "x": points[:, 0].round(5).tolist(),
        "y": points[:, 1].round(5).tolist(),
        "cluster": labels.tolist(),
        "terms": {str(k): v for k, v in landscape.label_clusters(labelled).items()},
    }
    return {
        "domain_id": domain_id,
        "model": model,
        "method": method,
        "params_hash": landscape.params_hash(ids, matrix, method, seed, min_cluster_size),
        "payload": json.dumps(payload),
    }


def store_landscape(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Upsert a `compute_landscape` row. Needs a writable, migrated connection; commits."""
    conn.execute(
        """INSERT INTO landscape_cache (domain_id, model, method, params_hash, computed_at, payload)
           VALUES (:domain_id, :model, :method, :params_hash, :computed_at, :payload)
           ON CONFLICT (domain_id, model, method) DO UPDATE SET
               params_hash = excluded.params_hash, computed_at = excluded.computed_at,
               payload = excluded.payload""",
        {**row, "computed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
    )
    conn.commit()


def _latest_kinds(conn: sqlite3.Connection, domain_id: str) -> dict[int, str]:
    """`kind` answer of each repo's latest verdict in the domain."""
    kinds: dict[int, str] = {}
    for r in conn.execute(
        "SELECT repo_id, answers_json FROM verdicts WHERE domain_id = ? ORDER BY decided_at, rowid", (domain_id,)
    ):
        try:
            kinds[r["repo_id"]] = json.loads(r["answers_json"])["kind"]["pred"]
        except (TypeError, KeyError, ValueError):
            kinds.pop(r["repo_id"], None)
    return kinds


def landscape_frame(
    conn: sqlite3.Connection,
    domain_id: str,
    model: str,
    method: str = LANDSCAPE_METHOD,
    seed: int = LANDSCAPE_SEED,
    min_cluster_size: int = LANDSCAPE_MIN_CLUSTER,
) -> tuple[pd.DataFrame | None, str]:
    """Cached landscape joined with repo attributes, plus a status.

    Status: `no_embeddings` (nothing to project), `not_computed` (no cache row or no table yet),
    `stale` (vectors or parameters changed since; the old points are still returned) or `fresh`.
    Never computes anything: the page offers a Recompute button for that.
    """
    from repka import landscape

    ids, matrix = _landscape_inputs(conn, domain_id, model)
    if not ids:
        return None, "no_embeddings"
    try:
        row = conn.execute(
            "SELECT params_hash, payload FROM landscape_cache WHERE domain_id = ? AND model = ? AND method = ?",
            (domain_id, model, method),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):  # "database is locked" and the like are real errors, not "not computed"
            raise
        row = None  # migration 003 not applied yet
    if row is None:
        return None, "not_computed"
    current = landscape.params_hash(ids, matrix, method, seed, min_cluster_size)
    status = "fresh" if row["params_hash"] == current else "stale"
    payload = json.loads(row["payload"])
    terms = payload["terms"]
    points = pd.DataFrame(
        {"repo_id": payload["repo_ids"], "x": payload["x"], "y": payload["y"], "cluster": payload["cluster"]}
    )
    kinds = _latest_kinds(conn, domain_id)
    points["kind"] = points["repo_id"].map(lambda i: _text(kinds.get(i) or "n/a"))
    points["cluster_name"] = points["cluster"].map(
        lambda c: "noise" if c == -1 else f"{c}: " + ", ".join(_text(t) for t in terms.get(str(c), []))
    )
    frame = repos_frame(conn, domain_id).merge(points, on="repo_id", how="inner")
    frame["description"] = frame["description"].fillna("")
    return frame, status


# --- Analytics page ---------------------------------------------------------------------------------

GAP_SCAN_FACTOR = 1.5  # a missed scan doubles the spacing, so 1.5 scan intervals is the break point
GAP_SLACK = timedelta(hours=6)  # run jitter: `repka run` is hourly and a due scan can start up to an hour late


def gap_after(scan_every: timedelta) -> timedelta:
    """Where a chart line breaks for a domain scanned every `scan_every`. A snapshot is taken once per scan,
    so regular spacing is one scan; a missed scan breaks the line (no interpolation)."""
    return scan_every * GAP_SCAN_FACTOR + GAP_SLACK


def window_deltas(
    conn: sqlite3.Connection, repo_ids: Iterable[int], days: int, now: datetime | None = None
) -> pd.DataFrame:
    """Stars gained over the last `days` per repo, from `store.metrics.stars_delta` (NaN = n/a)."""
    now = now or datetime.now(UTC)
    rows = []
    for repo_id in sorted(set(repo_ids)):
        snaps = [s for s in repo.snapshots_for(conn, repo_id) if s.taken_at <= now]
        if not snaps:
            continue
        rows.append(
            {
                "repo_id": repo_id,
                "full_name": _text(_full_name(conn, repo_id)),
                "stars": snaps[-1].stars,
                "stars_delta": stars_delta(snaps, now, days),
            }
        )
    frame = pd.DataFrame(rows, columns=_DELTA_COLUMNS)
    return frame.sort_values("stars_delta", ascending=False, na_position="last", kind="stable", ignore_index=True)


_DELTA_COLUMNS = ["repo_id", "full_name", "stars", "stars_delta"]


def _full_name(conn: sqlite3.Connection, repo_id: int) -> str | None:
    row = conn.execute("SELECT full_name FROM repos WHERE repo_id = ?", (repo_id,)).fetchone()
    return row["full_name"] if row else None


def with_gaps(frame: pd.DataFrame, group: str, time: str, value: str, gap: timedelta) -> pd.DataFrame:
    """Add an empty row just before each point that follows a gap longer than `gap` (see `gap_after`), so a
    line chart breaks there instead of drawing across the missing snapshots."""
    frame = frame.sort_values([group, time], kind="stable", ignore_index=True)
    starts_after_gap = frame.groupby(group)[time].diff() > gap
    breaks = frame[starts_after_gap].copy()
    if breaks.empty:
        return frame
    breaks[time] = breaks[time] - pd.Timedelta(seconds=1)
    breaks[value] = np.nan
    return pd.concat([frame, breaks], ignore_index=True).sort_values([group, time], kind="stable", ignore_index=True)


def sparkline_values(history: pd.DataFrame, column: str) -> dict[int, list[float]]:
    """Per repo, the non-null values of `column` in time order (for a table sparkline)."""
    out: dict[int, list[float]] = {}
    for repo_id, grp in history.dropna(subset=[column]).sort_values("taken_at").groupby("repo_id"):
        out[int(repo_id)] = [float(v) for v in grp[column]]
    return out


def velocity_table(repos: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """Repos ranked by velocity (n/a last), with the velocity trend per repo as a sparkline list."""
    trends = sparkline_values(history, "velocity")
    out = repos[["repo_id", "full_name", "stars", "velocity"]].copy()
    out["trend"] = out["repo_id"].map(lambda repo_id: trends.get(int(repo_id), []))
    return out.sort_values("velocity", ascending=False, na_position="last", kind="stable", ignore_index=True)


_POPULARITY_COLUMNS = ["taken_at", "dependency", "repos"]


def popularity_over_time(events: pd.DataFrame, top: int = 8) -> pd.DataFrame:
    """Repos using each dependency over time, from stack-history rows of many repos (columns: repo_id,
    dependency, change, first_seen_at). Keeps the `top` dependencies by their latest repo count."""
    if events.empty:
        return pd.DataFrame(columns=_POPULARITY_COLUMNS)
    ev = events.assign(
        step=events["change"].map({"added": 1, "removed": -1}),
        at=pd.to_datetime(events["first_seen_at"], utc=True),
    ).sort_values("at", kind="stable")
    state: dict[tuple[int, str], int] = {}
    seen: set[str] = set()
    rows = []
    for at, grp in ev.groupby("at", sort=True):
        for row in grp.itertuples(index=False):
            key = (int(row.repo_id), row.dependency)
            state[key] = state.get(key, 0) + int(row.step)
            seen.add(row.dependency)
        counts = Counter(dep for (_, dep), n in state.items() if n > 0)
        rows.extend({"taken_at": at, "dependency": dep, "repos": counts.get(dep, 0)} for dep in sorted(seen))
    frame = pd.DataFrame(rows, columns=_POPULARITY_COLUMNS)
    latest = frame[frame["taken_at"] == frame["taken_at"].max()].set_index("dependency")["repos"]
    keep = latest.sort_values(ascending=False, kind="stable").head(top).index
    return frame[frame["dependency"].isin(keep)].reset_index(drop=True)


def dependency_popularity(conn: sqlite3.Connection, repo_ids: Iterable[int], top: int = 8) -> pd.DataFrame:
    """`popularity_over_time` over the stack history of the given repos."""
    frames = []
    for repo_id in sorted(set(repo_ids)):
        frame = stack_history(conn, repo_id)
        if not frame.empty:
            frames.append(frame.assign(repo_id=repo_id))
    events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return popularity_over_time(events, top)
