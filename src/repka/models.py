"""Shared frozen value types (architecture § 6). Fields only; owning tasks may add defaulted fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RepoHit:
    repo_id: int
    node_id: str
    full_name: str
    html_url: str
    description: str | None
    language: str | None
    topics: tuple[str, ...]
    stars: int
    forks: int
    is_fork: bool
    created_at: datetime
    pushed_at: datetime
    archived: bool
    license: str | None


@dataclass(frozen=True)
class Snapshot:
    repo_id: int
    taken_at: datetime
    stars: int
    forks: int
    open_issues: int
    watchers: int
    contributors: int | None  # slow metric, refreshed every collect.slow_metrics_every
    commits_7d: int
    commits_30d: int
    active_weeks_12: int
    releases_90d: int
    latest_release_tag: str | None
    latest_release_at: datetime | None


@dataclass(frozen=True)
class KeyFile:
    repo_id: int
    path: str
    blob_oid: str
    text: str | None


@dataclass(frozen=True)
class Event:
    repo_id: int
    domain_id: str
    kind: str
    payload: dict[str, Any]
    detected_at: datetime


@dataclass(frozen=True)
class TriageItem:
    repo_id: int
    domain_id: str
    state: str
    input_hash: str


@dataclass(frozen=True)
class Verdict:
    repo_id: int
    domain_id: str
    model: str
    questions_hash: str
    input_hash: str
    answers: dict[str, Any]  # question id -> normalized answer (architecture § 6)
    accepted: bool


@dataclass(frozen=True)
class DigestItem:
    domain_id: str
    repo_id: int
    full_name: str
    html_url: str
    line_kind: str
    text_fields: dict[str, Any]
    rank_score: float


@dataclass(frozen=True)
class RepoMetrics:
    repo_id: int
    stars: int
    stars_delta_1d: int | None  # None = no baseline snapshot yet (never lifetime stars)
    stars_delta_7d: int | None
    stars_delta_30d: int | None
    velocity: float | None
