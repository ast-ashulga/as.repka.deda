"""Build, send and record exactly one digest per slot .

`send=False` is a pure preview: nothing is written. With `send=True` the slot is claimed (status `sending`)
BEFORE the message goes out and finalised after it: a crash or a failed write between the two can never lead
to a second message for the slot. A definite send failure releases the claim, so the slot stays open for the
next run; an unknown outcome (a timeout, a crash) keeps it closed, and the events stay unstamped, so their
items are shown again by the next digest instead of being lost.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from repka.config import AppConfig
from repka.digest.build import Digest, build_digest
from repka.digest.render import render_digest, split_messages
from repka.digest.slots import decide_slot, normalize_slot, recorded_slots, slot_key
from repka.domain.schema import Domain
from repka.models import DigestItem
from repka.notify.telegram_user import Sender
from repka.progress.active import stderr_line
from repka.store.db import iso_or_none

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
    delivery_unknown: bool = False  # failed, but Telegram may have delivered it: the slot is closed, not retried


def _chunks(ids: list[int]) -> Iterator[list[int]]:
    for i in range(0, len(ids), _CHUNK):
        yield ids[i : i + _CHUNK]


def _items(digest: Digest) -> Iterable[DigestItem]:
    for s in digest.sections:
        yield from (*s.new_repos, *s.releases, *s.movers, *s.stack_changes)


def _insert_digest(
    conn: sqlite3.Connection,
    slot: str,
    local_date: str,
    status: str,
    count: int,
    sent_at: str | None,
    kind: str = "main",
):
    """Record the slot. Tolerant of a row that already exists (a concurrent run recorded it first): no
    IntegrityError after a message was delivered; the existing row's id is returned. A non-main `kind` stores
    its slot as `slot_key(kind, slot)`, so it never collides with the main row of the same time."""
    key = slot_key(kind, slot)
    conn.execute(
        "INSERT OR IGNORE INTO digests (slot, local_date, status, item_count, sent_at, kind) VALUES (?, ?, ?, ?, ?, ?)",
        (key, local_date, status, count, sent_at, kind),
    )
    return conn.execute("SELECT id FROM digests WHERE slot = ? AND local_date = ?", (key, local_date)).fetchone()[0]


def _record_skipped(conn: sqlite3.Connection, skipped: list[tuple[str, str]], kind: str = "main") -> None:
    if not skipped:
        return
    with conn:
        for slot, day in skipped:
            conn.execute(
                "INSERT OR IGNORE INTO digests (slot, local_date, status, item_count, kind) VALUES (?, ?, 'skipped', 0, ?)",
                (slot_key(kind, slot), day, kind),
            )


def _claim_slot(conn: sqlite3.Connection, slot: str, local_date: str, count: int, kind: str) -> int:
    """Write the slot as `sending` (committed) before the message goes out; returns the row id."""
    with conn:
        return _insert_digest(conn, slot, local_date, "sending", count, None, kind)


def failure_note(out: DigestOutcome) -> str:
    """What happens to the slot after a failed send, for the CLI messages."""
    if out.delivery_unknown:
        return (
            "delivery could not be confirmed: the slot is closed and will not be retried, to avoid a duplicate; "
            "check the Telegram chat (`repka doctor` shows how to reopen the slot)"
        )
    return "nothing was delivered: the slot stays open for the next run"


def _finalize_claim(conn: sqlite3.Connection, digest_id: int, count: int, sent_at: str) -> None:
    """Turn a claimed row into the `sent` row (no-op for a row that was inserted as `sent` directly)."""
    conn.execute(
        "UPDATE digests SET status = 'sent', item_count = ?, sent_at = ? WHERE id = ? AND status IN ('sending', 'unknown')",
        (count, sent_at, digest_id),
    )


def send_and_record(
    conn: sqlite3.Connection,
    sender: Sender,
    parts: list[str],
    *,
    slot: str,
    local_date: str,
    count: int,
    kind: str,
    record: Callable[[], None],
) -> DigestOutcome:
    """Claim the slot, send, then run `record` (which must finalise the claim). Shared by the main digest and
    the releases message."""
    digest_id = _claim_slot(conn, slot, local_date, count, kind)
    # If send() raises or the process dies here, the claim stays: delivery is unknown, so never send again.
    result = sender.send(parts)
    if result.sent < 1:
        error = result.error or "nothing delivered"
        if result.delivery_unknown:
            with conn:
                conn.execute("UPDATE digests SET status = 'unknown' WHERE id = ? AND status = 'sending'", (digest_id,))
            stderr_line(f"warning: delivery of the {slot} message is unknown ({error}); it will not be retried.", "WARNING")
            return DigestOutcome(
                "failed", slot, local_date, count, parts, f"{error} (delivery unknown, not retried)", delivery_unknown=True
            )
        with conn:  # nothing went out: release the claim, the slot stays open for the next run
            conn.execute("DELETE FROM digests WHERE id = ? AND status = 'sending'", (digest_id,))
        return DigestOutcome("failed", slot, local_date, count, parts, error)
    try:
        record()
    except sqlite3.Error as exc:
        # Delivered but not recorded. The claim stays, so the next pass does not send it again.
        msg = f"sent but not recorded ({type(exc).__name__}: {exc}); the slot stays closed"
        stderr_line(f"warning: {slot} message {msg}", "WARNING")
        return DigestOutcome("sent", slot, local_date, count, parts, msg)
    # A part that failed after a delivered one still counts as sent; the error is kept for the caller.
    return DigestOutcome("sent", slot, local_date, count, parts, None if result.ok else result.error)


def _record_sent(conn: sqlite3.Connection, digest: Digest, now: datetime) -> None:
    """One transaction: the `sent` row, events stamped (shown + consumed), what each item showed."""
    with conn:
        digest_id = _insert_digest(conn, digest.slot, digest.local_date, "sent", digest.item_count, iso_or_none(now))
        _finalize_claim(conn, digest_id, digest.item_count, iso_or_none(now))
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
        slot = normalize_slot(slot)
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
    return send_and_record(
        conn, sender, parts, slot=digest.slot, local_date=digest.local_date, count=digest.item_count, kind="main",
        record=lambda: _record_sent(conn, digest, now),
    )  # fmt: skip
