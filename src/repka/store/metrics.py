"""Derived metrics: star deltas and commit/release velocity."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta

from repka.models import RepoMetrics, Snapshot
from repka.store.repo import snapshots_for


def stars_delta(snaps: Sequence[Snapshot], now: datetime, days: int) -> int | None:
    """Stars gained since the baseline: the latest snapshot taken at or before now - 0.9*days.

    None when there is no baseline, or when the baseline is the latest snapshot itself
    (one snapshot proves nothing about growth; lifetime stars must never pass as a delta).
    """
    usable = sorted((s for s in snaps if s.taken_at <= now), key=lambda s: s.taken_at)
    if len(usable) < 2:
        return None
    latest = usable[-1]
    cutoff = now - timedelta(days=0.9 * days)
    older = [s for s in usable[:-1] if s.taken_at <= cutoff]
    if not older:
        return None
    return latest.stars - older[-1].stars


def velocity(s: Snapshot) -> float | None:
    """ln(1 + commits_30d) * (0.5 + active_weeks_12 / 24) + 0.5 * ln(1 + releases_90d).

    Nulls count as 0 (a failed slow-metric refresh stores NULL), except when all three inputs are null.
    """
    commits, weeks, releases = s.commits_30d, s.active_weeks_12, s.releases_90d
    if commits is None and weeks is None and releases is None:
        return None
    commits, weeks, releases = commits or 0, weeks or 0, releases or 0
    return round(math.log1p(commits) * (0.5 + weeks / 24) + 0.5 * math.log1p(releases), 2)


def repo_metrics(conn: sqlite3.Connection, repo_id: int, now: datetime) -> RepoMetrics | None:
    snaps = [s for s in snapshots_for(conn, repo_id) if s.taken_at <= now]
    if not snaps:
        return None
    latest = max(snaps, key=lambda s: s.taken_at)
    return RepoMetrics(
        repo_id=repo_id,
        stars=latest.stars,
        stars_delta_1d=stars_delta(snaps, now, 1),
        stars_delta_7d=stars_delta(snaps, now, 7),
        stars_delta_30d=stars_delta(snaps, now, 30),
        velocity=velocity(latest),
    )
