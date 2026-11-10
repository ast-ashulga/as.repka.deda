"""Seed helpers for digest and metrics tests (a migrated temp DB, repos, snapshots, verdicts, events)."""

import json
from datetime import UTC, datetime

from repka.models import Event, RepoHit, Snapshot
from repka.store import repo
from repka.store.db import connect, migrate

DAY = "2026-11-01T00:00:00Z"


def make_db(path):
    conn = connect(path)
    migrate(conn)
    return conn


def add_repo(
    conn,
    repo_id,
    full_name=None,
    created="2026-10-20T00:00:00Z",
    description="a tool",
    stars=100,
    taken_at=datetime(2026, 11, 7, 6, 0, tzinfo=UTC),
    domain="d1",
    link=True,
):
    full_name = full_name or f"o/r{repo_id}"
    created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    hit = RepoHit(
        repo_id,
        f"R_{repo_id}",
        full_name,
        f"https://github.com/{full_name}",
        description,
        "Python",
        (),
        stars,
        0,
        False,
        created_at,
        created_at,
        False,
        None,
    )
    repo.upsert_repo(conn, hit, DAY)
    if link:
        repo.link_domain_repo(conn, domain, repo_id, "q", DAY)
    if stars is not None:
        add_snapshot(conn, repo_id, stars, taken_at)


def add_snapshot(conn, repo_id, stars, taken_at, commits_30d=5, active_weeks_12=6, releases_90d=1):
    repo.insert_snapshot(
        conn,
        Snapshot(
            repo_id,
            taken_at,
            stars,
            0,
            0,
            0,
            None,
            0,
            commits_30d,
            active_weeks_12,
            releases_90d,
            None,
            None,
        ),
    )


def add_verdict(
    conn,
    repo_id,
    accepted,
    domain="d1",
    substance=3,
    relevant=0.9,
    kind="tool",
    model="m",
    decided="2026-11-06T00:00:00Z",
    extra_answers: dict | None = None,
):
    answers = {
        "relevant": {"type": "noul", "p": relevant, "pred": True, "conf": relevant},
        "substance": {
            "type": "score",
            "pred": substance,
            "score": float(substance),
            "conf": 0.6,
            "probs": {},
        },
        "kind": {"type": "choice", "pred": kind, "conf": 0.7, "probs": {}},
    }
    answers.update(extra_answers or {})
    conn.execute(
        "INSERT INTO verdicts (repo_id, domain_id, model, questions_hash, input_hash, answers_json, accepted, decided_at)"
        " VALUES (?, ?, ?, 'q', 'i', ?, ?, ?)",
        (repo_id, domain, model, json.dumps(answers), int(accepted), decided),
    )


def add_event(
    conn,
    repo_id,
    kind,
    payload=None,
    domain="d1",
    at=datetime(2026, 11, 7, 5, 0, tzinfo=UTC),
):
    return repo.add_event(conn, Event(repo_id, domain, kind, payload or {}, at))
