"""The daily releases message: its own digest row, slot and send/record cycle (it keeps its own record of which releases were sent).

When `digest.releases_message` is on, the main digest leaves `release` events alone; this module owns them.
`build_releases` is a pure read like `build_digest`; `run_releases` stamps `events.digest_id` only after a real
delivery, in the same transaction as the `sent` row, so a failed send or a skipped slot loses nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from repka.config import AppConfig, DigestConfig
from repka.digest.build import current_stars, json_object, latest_verdicts
from repka.digest.render import link_html, safe_text, split_messages, star_text
from repka.digest.run import (
    DigestOutcome,
    _chunks,
    _finalize_claim,
    _insert_digest,
    _record_skipped,
    send_and_record,
)
from repka.digest.slots import decide_slot, normalize_slot, recorded_slots
from repka.domain.schema import Domain
from repka.models import DigestItem
from repka.notify.telegram_user import Sender
from repka.store.db import iso_or_none

KIND = "releases"
FOOTER = "Full list: repka releases --since 24h"


@dataclass
class ReleaseSection:
    domain_id: str
    domain_name: str
    items: list[DigestItem] = field(default_factory=list)
    more: int = 0  # releases not shown because of max_items_per_domain


@dataclass
class ReleasesDigest:
    slot: str
    local_date: str
    sections: list[ReleaseSection]
    shown_event_ids: list[int]
    consumed_event_ids: list[int]

    @property
    def item_count(self) -> int:
        return sum(len(s.items) for s in self.sections)


def _domain_section(
    conn: sqlite3.Connection, domain: Domain, cfg: DigestConfig, shown: list[int], consumed: list[int]
) -> ReleaseSection | None:
    verdicts = latest_verdicts(conn, domain.id)
    events = conn.execute(
        """
        SELECT e.id, e.repo_id, e.payload_json, r.full_name, r.html_url
        FROM events e JOIN repos r ON r.repo_id = e.repo_id
        WHERE e.digest_id IS NULL AND e.domain_id = ? AND e.kind = 'release' ORDER BY e.id
        """,
        (domain.id,),
    ).fetchall()

    by_repo: dict[int, list[sqlite3.Row]] = {}
    for ev in events:
        verdict = verdicts.get(ev["repo_id"])
        if verdict is None:  # untriaged: stays pending until the repo has a verdict
            continue
        if not verdict[0]:  # rejected: consumed, never shown
            consumed.append(ev["id"])
            continue
        by_repo.setdefault(ev["repo_id"], []).append(ev)

    cands: list[tuple[tuple[int, int], DigestItem, list[int], int]] = []
    for repo_id, evs in by_repo.items():
        latest = evs[-1]  # newest release event of the repo; the older ones are consumed with it
        payload = json_object(latest["payload_json"])
        stars = current_stars(conn, repo_id)
        item = DigestItem(
            domain.id,
            repo_id,
            latest["full_name"],
            latest["html_url"],
            "release",
            {"tag": payload.get("tag"), "name": payload.get("name"), "stars": stars},
            float(stars),
        )
        cands.append(((-stars, repo_id), item, [e["id"] for e in evs[:-1]], latest["id"]))

    section = ReleaseSection(domain.id, domain.name)
    room = max(cfg.max_items_per_domain, 0)
    for _key, item, older, latest_id in sorted(cands, key=lambda c: c[0]):
        consumed.extend(older)
        if room > 0:
            room -= 1
            section.items.append(item)
            shown.append(latest_id)
        else:
            section.more += 1
            consumed.append(latest_id)
    return section if section.items else None


def build_releases(
    conn: sqlite3.Connection,
    domains: list[Domain],
    now: datetime,
    cfg: DigestConfig,
    slot: str,
    local_date: str,
) -> ReleasesDigest:
    """Accepted repos' newest release per repo, by stars; rejected repos' release events are consumed unseen."""
    shown: list[int] = []
    consumed: list[int] = []
    sections = []
    for domain in domains:
        section = _domain_section(conn, domain, cfg, shown, consumed)
        if section is not None:
            sections.append(section)
    return ReleasesDigest(slot, local_date, sections, shown, consumed)


def _release_line(item: DigestItem) -> str:
    f = item.text_fields
    line = f"🚀 {link_html(item)} ★{star_text(int(f.get('stars') or 0))}"
    if f.get("tag"):
        line += f" · <code>{safe_text(f['tag'])}</code>"
    return line


def render_releases(d: ReleasesDigest) -> list[str]:
    """Message lines; empty when there is nothing to show."""
    if d.item_count == 0:
        return []
    lines = [f"<b>repka</b> · releases · {safe_text(d.local_date)}"]
    for s in d.sections:
        lines += ["", f"<b>{safe_text(s.domain_name)}</b> · {len(s.items) + s.more} releases"]
        lines += [_release_line(i) for i in s.items]
        if s.more:
            lines.append(f"…and {s.more} more")
    lines += ["", FOOTER]
    return lines


def _record_sent(conn: sqlite3.Connection, d: ReleasesDigest, now: datetime) -> None:
    """One transaction: the `sent` row, events stamped (shown + consumed), what each item showed."""
    with conn:
        digest_id = _insert_digest(conn, d.slot, d.local_date, "sent", d.item_count, iso_or_none(now), KIND)
        _finalize_claim(conn, digest_id, d.item_count, iso_or_none(now))
        for chunk in _chunks([*d.shown_event_ids, *d.consumed_event_ids]):
            marks = ",".join("?" * len(chunk))
            conn.execute(f"UPDATE events SET digest_id = ? WHERE id IN ({marks})", (digest_id, *chunk))
        conn.executemany(
            "INSERT OR IGNORE INTO digest_items (digest_id, repo_id, kind) VALUES (?, ?, ?)",
            [(digest_id, i.repo_id, i.line_kind) for s in d.sections for i in s.items],
        )


def run_releases(
    conn: sqlite3.Connection,
    domains: list[Domain],
    cfg: AppConfig,
    sender: Sender | None,
    now: datetime,
    send: bool,
    slot: str | None = None,
    tz: ZoneInfo | None = None,
) -> DigestOutcome:
    """Build, send and record the releases message for its slot (`digest.releases_slot`); mirrors `run_digest`."""
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be a timezone-aware UTC datetime")
    if send and sender is None:
        raise ValueError("send=True needs a sender")
    local = now.astimezone(tz)
    today = local.date().isoformat()
    recorded = recorded_slots(conn, (local.date() - timedelta(days=1)).isoformat(), kind=KIND)

    skipped: list[tuple[str, str]] = []
    if slot is not None:
        slot = normalize_slot(slot)
        due = None if (slot, today) in recorded else (slot, today)
    else:
        decision = decide_slot([cfg.digest.releases_slot], local, recorded, cfg.digest.max_lateness)
        due, skipped = decision.due, decision.skipped
    if send:
        _record_skipped(conn, skipped, KIND)
    if due is None:
        return DigestOutcome("not_due", None, None, 0)

    digest = build_releases(conn, domains, now.astimezone(UTC), cfg.digest, *due)
    parts = split_messages(render_releases(digest))
    if not send:
        return DigestOutcome("empty" if not parts else "preview", *due, digest.item_count, parts)
    if not parts:
        with conn:
            _insert_digest(conn, digest.slot, digest.local_date, "empty", 0, None, KIND)
        return DigestOutcome("empty", *due, 0)

    assert sender is not None
    return send_and_record(
        conn, sender, parts, slot=digest.slot, local_date=digest.local_date, count=digest.item_count, kind=KIND,
        record=lambda: _record_sent(conn, digest, now),
    )  # fmt: skip
