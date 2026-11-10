"""Which digest slot is due: coalescing of missed slots and a lateness limit (architecture § 5.5, RF5)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from repka.config import ConfigError


@dataclass(frozen=True)
class SlotDecision:
    due: tuple[str, str] | None  # (slot "HH:MM", local_date "YYYY-MM-DD")
    skipped: list[tuple[str, str]]


def parse_slot(slot: str) -> time:
    try:
        return datetime.strptime(slot, "%H:%M").time()
    except (ValueError, TypeError):
        raise ConfigError(f"bad slot {slot!r}: use HH:MM, 24-hour local time (e.g. 08:30)") from None


def decide_slot(
    slots: list[str],
    now_local: datetime,
    recorded: set[tuple[str, str]],
    max_lateness: timedelta,
) -> SlotDecision:
    """Candidates are today's and yesterday's passed, unrecorded slots.

    The latest candidate that is at most `max_lateness` old is due; every other candidate is skipped.
    """
    candidates: list[tuple[datetime, str, str]] = []
    for day in (now_local.date() - timedelta(days=1), now_local.date()):
        for slot in sorted(set(slots)):
            at = datetime.combine(day, parse_slot(slot), tzinfo=now_local.tzinfo)
            key = (slot, day.isoformat())
            if at <= now_local and key not in recorded:
                candidates.append((at, *key))
    candidates.sort()
    now_utc = now_local.astimezone(UTC)
    due = None
    if candidates and now_utc - candidates[-1][0].astimezone(UTC) <= max_lateness:
        due = (candidates[-1][1], candidates[-1][2])
    skipped = [(slot, day) for _at, slot, day in candidates if (slot, day) != due]
    return SlotDecision(due, skipped)


def slot_key(kind: str, slot: str) -> str:
    """The `digests.slot` value of a message: the slot itself for the main digest, `kind:slot` for any other."""
    return slot if kind == "main" else f"{kind}:{slot}"


def recorded_slots(conn: sqlite3.Connection, since_date: str, kind: str = "main") -> set[tuple[str, str]]:
    """(slot, local_date) of every recorded row of this kind (sent, empty or skipped) from `since_date` on.

    The kind prefix of `slot_key` is stripped, so callers compare plain "HH:MM" slots.
    """
    prefix = slot_key(kind, "")
    rows = conn.execute("SELECT slot, local_date FROM digests WHERE local_date >= ? AND kind = ?", (since_date, kind))
    return {(r["slot"].removeprefix(prefix), r["local_date"]) for r in rows}
