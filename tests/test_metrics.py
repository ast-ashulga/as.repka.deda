import math
from datetime import UTC, datetime, timedelta

from helpers_digest import add_repo

from repka.models import Snapshot
from repka.store.db import connect, migrate
from repka.store.metrics import repo_metrics, stars_delta, velocity
from repka.store.repo import insert_snapshot

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def S(stars=100, taken_at=NOW, commits_30d=0, active_weeks_12=0, releases_90d=0, repo_id=1) -> Snapshot:
    return Snapshot(
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
    )


def test_stars_delta_null_without_baseline():
    one_old = [S(stars=5000, taken_at=NOW - timedelta(days=40))]  # the only snapshot is also the latest
    for days in (1, 7, 30):
        assert stars_delta(one_old, NOW, days) is None
        assert stars_delta([S(stars=5000)], NOW, days) is None
    # a baseline for 7 days is not one for 30 days
    snaps = [S(stars=90, taken_at=NOW - timedelta(days=8)), S(stars=100)]
    assert stars_delta(snaps, NOW, 7) == 10
    assert stars_delta(snaps, NOW, 30) is None


def test_stars_delta_uses_latest_snapshot_older_than_0_9_days():
    snaps = [
        S(stars=50, taken_at=NOW - timedelta(days=3)),
        S(stars=80, taken_at=NOW - timedelta(days=0.95)),  # latest one at least 0.9 d old
        S(stars=95, taken_at=NOW - timedelta(days=0.5)),  # too fresh to be a 1d baseline
        S(stars=100),
    ]
    assert stars_delta(snaps, NOW, 1) == 20
    assert stars_delta(snaps, NOW, 7) is None


def test_velocity_formula():
    s = S(commits_30d=20, active_weeks_12=6, releases_90d=2)
    assert velocity(s) == round(math.log1p(20) * (0.5 + 6 / 24) + 0.5 * math.log1p(2), 2)


def test_velocity_all_null_is_none():
    assert velocity(S(commits_30d=None, active_weeks_12=None, releases_90d=None)) is None
    # a failed slow refresh stores NULL weeks: counts as 0, not None
    assert velocity(S(commits_30d=20, active_weeks_12=None, releases_90d=0)) == round(math.log1p(20) * 0.5, 2)


def test_repo_metrics_reads_snapshots(tmp_path):
    conn = connect(tmp_path / "m.db")
    migrate(conn)
    assert repo_metrics(conn, 1, NOW) is None
    add_repo(conn, 1, stars=None)
    insert_snapshot(conn, S(stars=90, taken_at=NOW - timedelta(days=2)))
    insert_snapshot(conn, S(stars=100, commits_30d=3))
    m = repo_metrics(conn, 1, NOW)
    assert (m.stars, m.stars_delta_1d, m.stars_delta_7d, m.stars_delta_30d) == (
        100,
        10,
        None,
        None,
    )
    assert m.velocity == round(math.log1p(3) * 0.5, 2)
