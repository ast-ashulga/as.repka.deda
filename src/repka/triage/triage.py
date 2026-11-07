"""Repository triage: decide the pending repos of a domain with a decider backend and store verdicts.

A verdict is keyed by (repo, domain, model, questions_hash) and carries the `input_hash` of the repo metadata
it was decided on, so changing the questions, the model or the description re-triages a repo, while a star
count moving does not. A repo without a verdict row is simply pending: a crashed worker, a failed item or a
rate-limited README fetch never produces an accepted or rejected verdict. The stored answers are kept, and each
triage pass re-applies the current `triage.thresholds` to them (`reclassify`), so a changed threshold needs no new run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from repka.config import ConfigError
from repka.github.client import GitHubError, RateLimited
from repka.domain.schema import Domain
from repka.models import TriageItem
from repka.textutil import printable, truncate
from repka.triage.deciders import DeciderBackend, QuestionSet, questions_hash

log = logging.getLogger("repka.triage")

CHUNK = 200  # items per backend call: bounds the memory of one worker run
README_TTL = timedelta(days=7)
README_MISSING = "(README not fetched yet)"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render_state(template: str, fields: dict[str, str]) -> str:
    """Fill `{name}` placeholders in ONE pass: a value that itself contains `{...}` is never expanded again
    (repository text is untrusted). Placeholders without a field are left as they are."""
    return _PLACEHOLDER.sub(lambda m: fields.get(m.group(1), m.group(0)), template)


def meta_hash(row: sqlite3.Row) -> str:
    """Identity of the repo metadata a verdict was decided on. Stars are left out on purpose."""
    parts = [row["full_name"], row["description"] or "", row["topics_json"] or "[]", row["language"] or ""]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def pending_repos(conn: sqlite3.Connection, domain_id: str, model: str, qhash: str) -> list[sqlite3.Row]:
    """Active repos of the domain with no verdict for (model, qhash), or one decided on other metadata."""
    rows = conn.execute(
        """
        SELECT r.*, v.input_hash AS verdict_input_hash,
               (SELECT s.stars FROM snapshots s WHERE s.repo_id = r.repo_id
                 ORDER BY s.taken_at DESC LIMIT 1) AS stars
        FROM domain_repos dr
        JOIN repos r ON r.repo_id = dr.repo_id
        LEFT JOIN verdicts v ON v.repo_id = dr.repo_id AND v.domain_id = dr.domain_id
                            AND v.model = ? AND v.questions_hash = ?
        WHERE dr.domain_id = ? AND r.status = 'active'
        ORDER BY r.repo_id
        """,
        (model, qhash, domain_id),
    ).fetchall()
    return [r for r in rows if r["verdict_input_hash"] is None or r["verdict_input_hash"] != meta_hash(r)]


def build_item(row: sqlite3.Row, readme: str, domain: Domain, readme_chars: int, template: str) -> TriageItem:
    """The model input for one repo. Everything that comes from outside goes through `printable`."""
    topics = ", ".join(json.loads(row["topics_json"] or "[]"))
    stars = row["stars"] if "stars" in row.keys() and row["stars"] is not None else "unknown"
    fields = {
        "full_name": row["full_name"],
        "description": row["description"] or "",
        "topics": topics,
        "language": row["language"] or "",
        "stars": str(stars),
        "is_fork": "yes" if row["is_fork"] else "no",
        "readme_excerpt": truncate(readme, readme_chars),
        "domain_profile": domain.profile,
    }
    state = render_state(template, {k: printable(v) for k, v in fields.items()})
    return TriageItem(repo_id=row["repo_id"], domain_id=domain.id, state=state, input_hash=meta_hash(row))


def store_readme(conn: sqlite3.Connection, repo_id: int, excerpt: str, now: str) -> None:
    """Keep the README excerpt so embeddings need no network and the next triage needs no refetch."""
    conn.execute(
        "UPDATE repos SET readme_excerpt = ?, readme_fetched_at = ? WHERE repo_id = ?", (excerpt, now, repo_id)
    )


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


def fresh_excerpt(row: sqlite3.Row, now: str) -> str | None:
    """The stored README excerpt if it was fetched less than 7 days ago (an empty README counts), else None."""
    fetched = row["readme_fetched_at"]
    if fetched is None or row["readme_excerpt"] is None:
        return None
    return row["readme_excerpt"] if _parse(now) - _parse(fetched) < README_TTL else None


def validate_thresholds(thresholds: dict[str, float], q: QuestionSet) -> None:
    """Every threshold must name an enabled noul or score question (a choice has no order to compare)."""
    if not thresholds:
        raise ConfigError("triage.thresholds is empty: nothing could ever be accepted.")
    for qid in thresholds:
        question = q.questions.get(qid)
        if question is None:
            raise ConfigError(
                f"triage.thresholds: {qid!r} is not an enabled question (have: {', '.join(q.questions)})."
            )
        if question["type"] not in ("noul", "score"):
            raise ConfigError(
                f"triage.thresholds: question {qid!r} is a {question['type']!r}; only noul and score can have a threshold."
            )


def is_accepted(answers: dict[str, dict], thresholds: dict[str, float]) -> bool:
    """noul: p >= t; score: pred >= t. All thresholds must pass; a missing answer fails."""
    for qid, t in thresholds.items():
        a = answers.get(qid)
        if a is None:
            return False
        value = a["p"] if a["type"] == "noul" else a["pred"]
        if value < t:
            return False
    return True


def reclassify(conn: sqlite3.Connection, domain_id: str, model: str, qhash: str, thresholds: dict[str, float]) -> int:
    """Re-apply the CURRENT thresholds to the stored answers of this model/questions (no model run), so editing
    `triage.thresholds` takes effect on the next triage pass. Returns how many verdicts flipped."""
    rows = conn.execute(
        "SELECT rowid, accepted, answers_json FROM verdicts "
        "WHERE domain_id = ? AND model = ? AND questions_hash = ? AND answers_json IS NOT NULL",
        (domain_id, model, qhash),
    ).fetchall()
    flipped = 0
    for r in rows:
        accepted = int(is_accepted(json.loads(r["answers_json"]), thresholds))
        if accepted != r["accepted"]:
            conn.execute("UPDATE verdicts SET accepted = ? WHERE rowid = ?", (accepted, r["rowid"]))
            flipped += 1
    conn.commit()
    return flipped


@dataclass
class TriageStats:
    pending: int = 0
    decided: int = 0
    accepted: int = 0
    errors: int = 0
    rate_limited: bool = False  # a README fetch hit the rate limit: the repos that needed one stay pending
    decisions: list[tuple[str, dict[str, dict], bool]] = field(default_factory=list)  # (full_name, answers, accepted)


def _rid(item: TriageItem) -> str:
    return f"{item.domain_id}:{item.repo_id}"


def _save_verdict(
    conn: sqlite3.Connection,
    domain_id: str,
    row: sqlite3.Row,
    model: str,
    qhash: str,
    answers: dict[str, dict],
    accepted: bool,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO verdicts (repo_id, domain_id, model, questions_hash, input_hash, answers_json, accepted, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (repo_id, domain_id, model, questions_hash) DO UPDATE SET
            input_hash = excluded.input_hash,
            answers_json = excluded.answers_json,
            accepted = excluded.accepted,
            decided_at = excluded.decided_at
        """,
        (row["repo_id"], domain_id, model, qhash, meta_hash(row), json.dumps(answers), int(accepted), now),
    )


def triage_domain(
    conn: sqlite3.Connection,
    domain: Domain,
    backend: DeciderBackend,
    q: QuestionSet,
    model: str,
    thresholds: dict[str, float],
    readme_fn: Callable[[str], str],
    readme_chars: int,
    now: str,
    limit: int | None = None,
) -> TriageStats:
    """Decide the pending repos of `domain` (at most `limit`), chunk by chunk.

    Failure blame: a repo only gets a verdict row when the backend answered ALL its questions. A WorkerError
    propagates after nothing was written for its chunk (earlier chunks and fetched READMEs are kept), and a
    per-item error, a missing answer or a rate-limited README fetch just leaves that repo pending (RF7, RF9).
    """
    qhash = questions_hash(q)
    reclassify(conn, domain.id, model, qhash, thresholds)
    pending = pending_repos(conn, domain.id, model, qhash)
    if limit is not None:
        pending = pending[:limit]
    stats = TriageStats(pending=len(pending))
    for start in range(0, len(pending), CHUNK):
        rows: dict[str, sqlite3.Row] = {}
        items: list[TriageItem] = []
        for row in pending[start : start + CHUNK]:
            excerpt = fresh_excerpt(row, now)
            if excerpt is None:
                if stats.rate_limited:
                    continue  # no further fetches this run; stays pending
                try:
                    text = readme_fn(row["full_name"])
                except RateLimited as exc:
                    log.warning("README fetch rate limited (%s); repos that need one stay pending", exc)
                    stats.rate_limited = True
                    continue
                except GitHubError as exc:  # this repo only; AuthError is a ConfigError and propagates
                    log.warning("README of %s not fetched (%s); it stays pending", row["full_name"], exc)
                    stats.errors += 1
                    continue
                excerpt = truncate(printable(text), readme_chars)
                store_readme(conn, row["repo_id"], excerpt, now)
            item = build_item(row, excerpt, domain, readme_chars, q.state_template)
            items.append(item)
            rows[_rid(item)] = row
        conn.commit()  # fetched READMEs are kept even if the worker fails below
        if not items:
            continue
        answers_by_rid, errors_by_rid = backend.decide(items, q)  # WorkerError propagates: nothing written yet
        for item in items:
            rid, row = _rid(item), rows[_rid(item)]
            answers = answers_by_rid.get(rid)
            if answers is None or rid in errors_by_rid:
                log.warning("%s stays pending: %s", row["full_name"], errors_by_rid.get(rid, "no answer from the backend"))
                stats.errors += 1
                continue
            accepted = is_accepted(answers, thresholds)
            _save_verdict(conn, domain.id, row, model, qhash, answers, accepted, now)
            stats.decided += 1
            stats.accepted += int(accepted)
            stats.decisions.append((row["full_name"], answers, accepted))
        conn.commit()
    return stats
