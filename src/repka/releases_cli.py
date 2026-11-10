"""`repka releases [--since DURATION] [--domain ID]`: recent release events of accepted repos.

Read-only: nothing is sent, marked or recorded. Sent and pending events both count, so this shows
what happened in the window whether or not a digest already carried it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from repka import config
from repka.config import ConfigError, parse_duration
from repka.digest.build import _latest_verdicts, _stars
from repka.domain.schema import Domain, load_domains
from repka.store.db import connect
from repka.store.repo import _iso
from repka.textutil import printable

DEFAULT_SINCE = "24h"


@dataclass(frozen=True)
class _Release:
    stars: int
    repo_id: int
    full_name: str
    tag: str
    published_at: str


def _releases(conn: sqlite3.Connection, domain: Domain, since: datetime) -> list[_Release]:
    """Latest release event per repo in the window, accepted repos only, most stars first."""
    verdicts = _latest_verdicts(conn, domain.id)
    rows = conn.execute(
        """
        SELECT e.repo_id, e.payload_json, r.full_name
        FROM events e JOIN repos r ON r.repo_id = e.repo_id
        WHERE e.domain_id = ? AND e.kind = 'release' AND e.detected_at >= ?
        ORDER BY e.id
        """,
        (domain.id, _iso(since)),
    ).fetchall()
    latest: dict[int, sqlite3.Row] = {}
    for row in rows:  # ascending id: the last row of a repo is its newest event
        latest[row["repo_id"]] = row
    out = []
    for repo_id, row in latest.items():
        verdict = verdicts.get(repo_id)
        if verdict is None or not verdict[0]:  # untriaged or rejected: never shown
            continue
        payload = _payload(row["payload_json"])
        out.append(
            _Release(
                stars=_stars(conn, repo_id),
                repo_id=repo_id,
                full_name=printable(str(row["full_name"])),
                tag=printable(str(payload.get("tag") or "-")),
                published_at=printable(str(payload.get("published_at") or "-")),
            )
        )
    return sorted(out, key=lambda r: (-r.stars, r.repo_id))


def _payload(text: str | None) -> dict:
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _cmd_releases(args: argparse.Namespace) -> int:
    since = parse_duration(args.since)
    path = config.db_path()
    if not path.exists():
        raise ConfigError(f"no database at {path}; run `repka db migrate` first")
    domains = [d for d in load_domains() if d.enabled]
    if args.domain is not None:
        domains = [d for d in domains if d.id == args.domain]
        if not domains:
            raise ConfigError(f"no enabled domain {args.domain!r} (see: repka domain list)")
    if not domains:
        print("no enabled domains")
        return 0
    cutoff = datetime.now(UTC) - since
    conn = connect(readonly=True)
    try:
        for index, domain in enumerate(domains):
            if index:
                print()
            print(f"== {printable(domain.name)} ({printable(domain.id)}) ==")
            found = _releases(conn, domain, cutoff)
            if not found:
                print(f"no releases in the last {args.since}")
            for rel in found:
                print(f"★{rel.stars}  {rel.full_name}  {rel.tag}  {rel.published_at}")
    finally:
        conn.close()
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    releases = sub.add_parser(
        "releases",
        help="list recent release events of accepted repos (read-only)",
        description="List the latest release event per accepted repo, one section per enabled domain, "
        "most stars first. Shows sent and pending events alike; nothing is sent or marked.",
    )
    releases.add_argument("--since", metavar="DURATION", default=DEFAULT_SINCE, help="window: 30m, 12h or 7d (default 24h)")
    releases.add_argument("--domain", metavar="ID", help="only this enabled domain")
    releases.set_defaults(func=_cmd_releases)
