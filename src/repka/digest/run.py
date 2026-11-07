"""Build, send and record exactly one digest per slot (architecture § 5.5).

`send=False` is a pure preview: nothing is written. With `send=True` the slot is recorded only
after a real delivery (or as `empty`), so a failed send leaves the slot open for the next run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from repka.config import AppConfig
from repka.digest.build import Digest, build_digest
from repka.digest.render import render_digest, split_messages
from repka.digest.slots import decide_slot, parse_slot, recorded_slots
from repka.domain.schema import Domain
from repka.models import DigestItem
from repka.notify.telegram_user import Sender
from repka.store.repo import _iso

Status = Literal["not_due", "empty", "preview", "sent", "failed"]
_CHUNK = 500


@dataclass
class DigestOutcome:
    status: Status
    slot: str | None
    local_date: str | None
    item_count: int
    parts: list[str] = field(default_factory=list)
    error: str | None = None


def _chunks(ids: list[int]) -> Iterator[list[int]]:
    for i in range(0, len(ids), _CHUNK):
        yield ids[i : i + _CHUNK]


def _items(digest: Digest) -> Iterable[DigestItem]:
    for s in digest.sections:
        yield from (*s.new_repos, *s.releases, *s.movers, *s.stack_changes)


def _insert_digest(conn: sqlite3.Connection, slot: str, local_date: str, status: str, count: int, sent_at: str | None):
    cur = conn.execute(
        "INSERT INTO digests (slot, local_date, status, item_count, sent_at) VALUES (?, ?, ?, ?, ?)",
        (slot, local_date, status, count, sent_at),
    )
    return cur.lastrowid


def _record_skipped(conn: sqlite3.Connection, skipped: list[tuple[str, str]]) -> None:
    if not skipped:
        return
    with conn:
        for slot, day in skipped:
            conn.execute(
                "INSERT OR IGNORE INTO digests (slot, local_date, status, item_count) VALUES (?, ?, 'skipped', 0)",
                (slot, day),
            )


def _record_sent(conn: sqlite3.Connection, digest: Digest, now: datetime) -> None:
    """One transaction: the `sent` row, events stamped (shown + consumed), what each item showed."""
    with conn:
        digest_id = _insert_digest(conn, digest.slot, digest.local_date, "sent", digest.item_count, _iso(now))
        for chunk in _chunks([*digest.shown_event_ids, *digest.consumed_event_ids]):
            marks = ",".join("?" * len(chunk))
            conn.execute(f"UPDATE events SET digest_id = ? WHERE id IN ({marks})", (digest_id, *chunk))
        conn.executemany(
            "INSERT OR IGNORE INTO digest_items (digest_id, repo_id, kind) VALUES (?, ?, ?)",
            [(digest_id, i.repo_id, i.line_kind) for i in _items(digest)],
        )


def run_digest(
    conn: sqlite3.Connection,
    domains: list[Domain],
    cfg: AppConfig,
    sender: Sender | None,
    now: datetime,
    send: bool,
    slot: str | None = None,
    tz: ZoneInfo | None = None,
) -> DigestOutcome:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be a timezone-aware UTC datetime")
    if send and sender is None:
        raise ValueError("send=True needs a sender")
    local = now.astimezone(tz)
    today = local.date().isoformat()
    recorded = recorded_slots(conn, (local.date() - timedelta(days=1)).isoformat())

    skipped: list[tuple[str, str]] = []
    if slot is not None:
        parse_slot(slot)
        due = None if (slot, today) in recorded else (slot, today)
    else:
        decision = decide_slot(cfg.digest.slots, local, recorded, cfg.digest.max_lateness)
        due, skipped = decision.due, decision.skipped
    if send:
        _record_skipped(conn, skipped)
    if due is None:
        return DigestOutcome("not_due", None, None, 0)

    digest = build_digest(conn, domains, now, cfg.digest, *due)
    parts = split_messages(render_digest(digest))
    if not send:
        return DigestOutcome("empty" if not parts else "preview", *due, digest.item_count, parts)
    if not parts:
        with conn:
            _insert_digest(conn, digest.slot, digest.local_date, "empty", 0, None)
        return DigestOutcome("empty", *due, 0)

    assert sender is not None
    result = sender.send(parts)
    if result.sent < 1:
        return DigestOutcome("failed", *due, digest.item_count, parts, result.error or "nothing delivered")
    _record_sent(conn, digest, now)
    # A part that failed after a delivered one still counts as sent; the error is kept for the caller.
    return DigestOutcome("sent", *due, digest.item_count, parts, None if result.ok else result.error)
