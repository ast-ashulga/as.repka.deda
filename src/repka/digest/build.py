"""Digest selection: which events and star movers are shown, per domain (architecture § 5.5).

Pure read: nothing is written. "Consumed" only means an event id is listed in
`Digest.consumed_event_ids`; the sender stamps `events.digest_id` after a real send.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from repka.config import DigestConfig
from repka.domain.schema import Domain
from repka.models import DigestItem
from repka.store.metrics import repo_metrics
from repka.store.repo import _iso, _parse, latest_snapshot

MOVER_REPEAT_WINDOW = timedelta(hours=24)
BASE_QUESTIONS = frozenset({"relevant", "substance", "kind", "is_junk"})
MAX_FACETS = 3


def facet_tags(answers: dict[str, Any], min_p: float) -> list[tuple[str, float]]:
    """Domain-specific `noul` answers (not the base questions) with p >= min_p: (question id, p), best first."""
    tags: list[tuple[str, float]] = []
    for qid, ans in answers.items():
        if qid in BASE_QUESTIONS or not isinstance(ans, dict) or ans.get("type") != "noul":
            continue
        p = ans.get("p")
        if isinstance(p, (int, float)) and not isinstance(p, bool) and p >= min_p:
            tags.append((qid, float(p)))
    tags.sort(key=lambda t: (-t[1], t[0]))
    return tags[:MAX_FACETS]


@dataclass
class DigestSection:
    domain_id: str
    domain_name: str
    new_repos: list[DigestItem] = field(default_factory=list)  # line_kind "new" or "found"
    releases: list[DigestItem] = field(default_factory=list)  # "release"
    movers: list[DigestItem] = field(default_factory=list)  # "mover"
    stack_changes: list[DigestItem] = field(default_factory=list)  # "stack"
    more: int = 0  # items not shown because of max_items_per_domain
    more_by_kind: dict[str, int] = field(default_factory=dict)  # `more` split by line_kind

    @property
    def item_count(self) -> int:
        return len(self.new_repos) + len(self.releases) + len(self.movers) + len(self.stack_changes)


@dataclass
class Digest:
    slot: str
    local_date: str
    sections: list[DigestSection]
    shown_event_ids: list[int]
    consumed_event_ids: list[int]
    insights: bool = False  # built with digest.insights on (the renderer picks the new format)
    facet_min_p: float = 0.7  # decider-answer threshold behind the tags (shown in the legend)

    @property
    def item_count(self) -> int:
        return sum(s.item_count for s in self.sections)


# A candidate: sort key (smaller sorts first), the item, and the events it stands for.
_Cand = tuple[tuple, DigestItem, list[int]]


def _json(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _latest_verdicts(conn: sqlite3.Connection, domain_id: str) -> dict[int, tuple[bool, dict[str, Any]]]:
    """repo_id -> (accepted, answers) of the latest verdict for this domain."""
    out: dict[int, tuple[bool, dict[str, Any]]] = {}
    for r in conn.execute(
        "SELECT repo_id, accepted, answers_json FROM verdicts WHERE domain_id = ? ORDER BY decided_at, rowid",
        (domain_id,),
    ):
        out[r["repo_id"]] = (bool(r["accepted"]), _json(r["answers_json"]))
    return out


def _num(answers: dict[str, Any], question: str, key: str) -> float:
    value = (answers.get(question) or {}).get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _kind_level(answers: dict[str, Any]) -> tuple[str | None, int | None]:
    """(kind, level) from the `kind` and `substance` answers; None for anything malformed."""
    kind = answers["kind"].get("pred") if isinstance(answers.get("kind"), dict) else None
    level = answers["substance"].get("pred") if isinstance(answers.get("substance"), dict) else None
    return (
        kind if isinstance(kind, str) else None,
        level if isinstance(level, int) and not isinstance(level, bool) else None,
    )


def _recent_movers(conn: sqlite3.Connection, now: datetime) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT di.repo_id FROM digest_items di JOIN digests d ON d.id = di.digest_id
        WHERE di.kind = 'mover' AND d.status = 'sent' AND d.sent_at >= ?
        """,
        (_iso(now - MOVER_REPEAT_WINDOW),),
    )
    return {r["repo_id"] for r in rows}


def _stars(conn: sqlite3.Connection, repo_id: int) -> int:
    snap = latest_snapshot(conn, repo_id)
    return snap.stars if snap else 0


def _sorted(cands: list[_Cand]) -> list[_Cand]:
    return sorted(cands, key=lambda c: c[0])


def _domain_section(
    conn: sqlite3.Connection,
    domain: Domain,
    now: datetime,
    cfg: DigestConfig,
    recent_movers: set[int],
    shown: list[int],
    consumed: list[int],
) -> DigestSection | None:
    verdicts = _latest_verdicts(conn, domain.id)
    events = conn.execute(
        """
        SELECT e.id, e.repo_id, e.kind, e.payload_json, e.detected_at, r.full_name, r.html_url, r.description, r.created_at
        FROM events e JOIN repos r ON r.repo_id = e.repo_id
        WHERE e.digest_id IS NULL AND e.domain_id = ? ORDER BY e.id
        """,
        (domain.id,),
    ).fetchall()

    # repo_id -> events, per kind; rejected -> consumed, untriaged -> left pending
    by_kind: dict[str, dict[int, list[sqlite3.Row]]] = {
        "new_repo": {},
        "release": {},
        "stack_changed": {},
    }
    pending_cutoff = now - cfg.pending_max_age
    for ev in events:
        if cfg.releases_message and ev["kind"] == "release":
            continue  # owned by the releases message: not shown and not consumed here, rejected repos included
        verdict = verdicts.get(ev["repo_id"])
        if verdict is None:
            continue
        if not verdict[0] or ev["kind"] not in by_kind:  # rejected, or key_file_changed (shown via stack_changed)
            consumed.append(ev["id"])
            continue
        if cfg.insights and ev["kind"] == "new_repo":
            detected = _parse(ev["detected_at"])
            if detected is not None and detected < pending_cutoff:  # waited too long: drop silently
                consumed.append(ev["id"])
                continue
        by_kind[ev["kind"]].setdefault(ev["repo_id"], []).append(ev)

    def answers(repo_id: int) -> dict[str, Any]:
        return verdicts[repo_id][1]

    new_cands: list[_Cand] = []
    cutoff = now - cfg.new_max_age
    for repo_id, evs in by_kind["new_repo"].items():
        ev = evs[0]
        a = answers(repo_id)
        stars = _stars(conn, repo_id)
        created = _parse(ev["created_at"])
        is_new = created is not None and created >= cutoff
        kind, level = _kind_level(a)
        fields: dict[str, Any] = {
            "description": ev["description"],
            "stars": stars,
            "kind": kind,
            "level": level,
        }
        if not is_new and created is not None:
            fields["since"] = created.year
        if cfg.insights:
            fields["facets"] = facet_tags(a, cfg.facet_min_p)
        sub, rel = _num(a, "substance", "pred"), _num(a, "relevant", "p")
        item = DigestItem(
            domain.id,
            repo_id,
            ev["full_name"],
            ev["html_url"],
            "new" if is_new else "found",
            fields,
            sub,
        )
        new_cands.append(((-sub, -rel, -stars, repo_id), item, [e["id"] for e in evs]))

    release_cands: list[_Cand] = []
    for repo_id, evs in by_kind["release"].items():
        payload = _json(evs[-1]["payload_json"])  # newest release event
        stars = _stars(conn, repo_id)
        item = DigestItem(
            domain.id,
            repo_id,
            evs[-1]["full_name"],
            evs[-1]["html_url"],
            "release",
            {"tag": payload.get("tag"), "name": payload.get("name")},
            float(stars),
        )
        release_cands.append(((-stars, repo_id), item, [e["id"] for e in evs]))

    stack_cands: list[_Cand] = []
    for repo_id, evs in by_kind["stack_changed"].items():
        changes = []
        for e in evs:
            p = _json(e["payload_json"])
            changes.append(
                {
                    "path": p.get("path"),
                    "added": p.get("added") or [],
                    "removed": p.get("removed") or [],
                }
            )
        stars = _stars(conn, repo_id)
        item = DigestItem(
            domain.id,
            repo_id,
            evs[0]["full_name"],
            evs[0]["html_url"],
            "stack",
            {"changes": changes},
            float(stars),
        )
        stack_cands.append(((-stars, repo_id), item, [e["id"] for e in evs]))

    mover_cands: list[_Cand] = []
    for repo_id, (accepted, a) in verdicts.items():
        if not accepted or repo_id in recent_movers:
            continue
        row = conn.execute(
            """
            SELECT r.full_name, r.html_url, r.description FROM repos r JOIN domain_repos dr ON dr.repo_id = r.repo_id
            WHERE dr.domain_id = ? AND r.repo_id = ?
            """,
            (domain.id, repo_id),
        ).fetchone()
        if row is None:
            continue
        m = repo_metrics(conn, repo_id, now)
        if m is None or m.stars_delta_1d is None or m.stars_delta_1d < cfg.star_spike_min_delta:
            continue
        mover_fields: dict[str, Any] = {
            "stars_delta_1d": m.stars_delta_1d,
            "velocity": m.velocity,
            "stars": m.stars,
        }
        if cfg.insights:
            kind, level = _kind_level(a)
            mover_fields.update(
                description=row["description"],
                kind=kind,
                level=level,
                facets=facet_tags(a, cfg.facet_min_p),
            )
        item = DigestItem(
            domain.id,
            repo_id,
            row["full_name"],
            row["html_url"],
            "mover",
            mover_fields,
            float(m.stars_delta_1d),
        )
        mover_cands.append(((-m.stars_delta_1d, repo_id), item, []))

    section = DigestSection(domain.id, domain.name)
    room = max(cfg.max_items_per_domain, 0)
    new_group = (new_cands, section.new_repos)
    release_group = (release_cands, section.releases)
    mover_group = (mover_cands, section.movers)
    stack_group = (stack_cands, section.stack_changes)
    if cfg.insights:  # releases last, sharing the per-domain limit
        groups = (new_group, mover_group, stack_group, release_group)
    else:
        groups = (new_group, release_group, mover_group, stack_group)
    for cands, target in groups:
        for _key, item, ids in _sorted(cands):
            if room > 0:
                room -= 1
                target.append(item)
                shown.extend(ids)
            else:
                section.more += 1
                section.more_by_kind[item.line_kind] = section.more_by_kind.get(item.line_kind, 0) + 1
                if not (cfg.insights and target is section.new_repos):  # insights: new overflow stays pending
                    consumed.extend(ids)
    return section if section.item_count else None


def build_digest(
    conn: sqlite3.Connection,
    domains: list[Domain],
    now: datetime,
    cfg: DigestConfig,
    slot: str,
    local_date: str,
) -> Digest:
    now = now.astimezone(UTC)
    shown: list[int] = []
    consumed: list[int] = []
    recent = _recent_movers(conn, now)
    sections = []
    for domain in domains:
        section = _domain_section(conn, domain, now, cfg, recent, shown, consumed)
        if section is not None:
            sections.append(section)
    return Digest(slot, local_date, sections, shown, consumed, cfg.insights, cfg.facet_min_p)


def pending_overflow(
    conn: sqlite3.Connection,
    domains: list[Domain],
    now: datetime,
    cfg: DigestConfig,
    slot: str,
    local_date: str,
) -> Digest:
    """The items a normal build holds back (`max_items_per_domain`), as a digest of their own.

    Builds twice, without the limit and as configured, and subtracts by (domain, repo, line kind).
    Pure read, like `build_digest`; the event id lists are empty and no section carries a `more`.
    """
    everything = build_digest(conn, domains, now, replace(cfg, max_items_per_domain=10**9), slot, local_date)
    normal = build_digest(conn, domains, now, cfg, slot, local_date)
    shown = {
        (i.domain_id, i.repo_id, i.line_kind)
        for s in normal.sections
        for i in (*s.new_repos, *s.releases, *s.movers, *s.stack_changes)
    }

    def held(items: list[DigestItem]) -> list[DigestItem]:
        return [i for i in items if (i.domain_id, i.repo_id, i.line_kind) not in shown]

    sections = []
    for s in everything.sections:
        left = DigestSection(
            s.domain_id,
            s.domain_name,
            new_repos=held(s.new_repos),
            releases=held(s.releases),
            movers=held(s.movers),
            stack_changes=held(s.stack_changes),
        )
        if left.item_count:
            sections.append(left)
    return Digest(slot, local_date, sections, [], [], cfg.insights, cfg.facet_min_p)
