"""Analytics page: its data shows store.metrics values, gaps stay gaps, and the page runs on a scratch home.

Needs pandas and streamlit: run with `uv run --group dashboard pytest`.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("streamlit")

from helpers_digest import add_repo, add_snapshot, add_verdict, make_db  # noqa: E402

import repka  # noqa: E402
from repka import dashboard_data as data  # noqa: E402
from repka.models import KeyFile  # noqa: E402
from repka.store import repo  # noqa: E402
from repka.store.metrics import repo_metrics, stars_delta  # noqa: E402

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
PAGE = Path(repka.__file__).resolve().parents[2] / "dashboard" / "pages" / "3_Analytics.py"


def _seed(conn):
    d = lambda day: datetime(2026, 11, day, 6, 0, tzinfo=UTC)  # noqa: E731
    add_repo(conn, 1, full_name="o/alpha", stars=90, taken_at=datetime(2026, 10, 30, 6, tzinfo=UTC))
    add_snapshot(conn, 1, 100, d(7), commits_30d=20, active_weeks_12=12, releases_90d=3)
    add_verdict(conn, 1, accepted=True)
    add_repo(conn, 2, full_name="o/beta", stars=10, taken_at=d(7))
    add_verdict(conn, 2, accepted=True)
    repo.insert_key_file(conn, KeyFile(1, "pyproject.toml", "oid1", None), ["a", "b"], "2026-11-01T00:00:00Z")
    repo.insert_key_file(conn, KeyFile(1, "pyproject.toml", "oid2", None), ["b", "c"], "2026-11-05T00:00:00Z")
    repo.insert_key_file(conn, KeyFile(2, "pyproject.toml", "oid3", None), ["b"], "2026-11-05T00:00:00Z")
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    conn = make_db(tmp_path / "repka.db")
    _seed(conn)
    yield conn
    conn.close()


def test_page_values_equal_store_metrics(conn):
    repos = data.repos_frame(conn, "d1", accepted_only=True, now=NOW)
    movers = data.window_deltas(conn, repos["repo_id"], 7, now=NOW).set_index("repo_id")
    history = data.metrics_history(conn, repos["repo_id"])
    velocity = data.velocity_table(repos, history).set_index("repo_id")
    for repo_id in (1, 2):
        metrics = repo_metrics(conn, repo_id, NOW)
        assert pd.isna(movers.loc[repo_id, "stars_delta"]) == (metrics.stars_delta_7d is None)
        if metrics.stars_delta_7d is not None:
            assert movers.loc[repo_id, "stars_delta"] == metrics.stars_delta_7d
        assert velocity.loc[repo_id, "velocity"] == metrics.velocity or (
            pd.isna(velocity.loc[repo_id, "velocity"]) and metrics.velocity is None
        )
    assert movers.loc[1, "stars_delta"] == 10  # baseline 10-30 (90 stars), latest 11-07 (100)


def test_with_gaps_breaks_lines_without_interpolating():
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    frame = pd.DataFrame(
        {
            "repo_id": [1, 1, 1, 2],
            "taken_at": pd.to_datetime([t0, t0 + timedelta(days=1), t0 + timedelta(days=10), t0], utc=True),
            "stars": [1, 2, 3, 5],
        }
    )
    out = data.with_gaps(frame, "repo_id", "taken_at", "stars")
    repo1 = out[out["repo_id"] == 1].reset_index(drop=True)
    assert len(repo1) == 4  # one empty row inserted before the 10-day-later snapshot
    assert pd.isna(repo1.loc[2, "stars"]) and repo1.loc[3, "stars"] == 3
    assert len(out[out["repo_id"] == 2]) == 1  # a single snapshot gets no break


def test_null_deltas_stay_null_in_history(conn):
    frame = data.metrics_history(conn, [2])
    assert len(frame) == 1
    assert pd.isna(frame.iloc[0]["stars_delta_7d"])  # one snapshot: n/a, not 0 and not lifetime stars


def test_stars_delta_helper_agrees_with_window_helper(conn):
    snaps = repo.snapshots_for(conn, 1)
    assert stars_delta(snaps, NOW, 7) == 10


def test_popularity_counts_repos_per_dependency(conn):
    frame = data.dependency_popularity(conn, [1, 2], top=8)
    last = frame[frame["taken_at"] == frame["taken_at"].max()].set_index("dependency")["repos"]
    assert last["b"] == 2  # b is in both repos
    assert last["a"] == 0 and last["c"] == 1  # a removed from repo 1 in the second version
    assert data.dependency_popularity(conn, [], top=8).empty


def test_analytics_page_runs_on_scratch_home(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("REPKA_HOME", str(tmp_path))
    monkeypatch.delenv("REPKA_GITHUB_TOKEN", raising=False)
    (tmp_path / ".state").mkdir()
    (tmp_path / "domains").mkdir()
    (tmp_path / "domains" / "d1.yaml").write_text(
        'id: d1\nname: "Domain one"\nenabled: true\nscan_every: 24h\nprofile: test\n'
        "search:\n  keywords:\n    - [\"mcp\"]\n  topics: []\n  languages: []\n  exclude: []\n"
        "key_files: [pyproject.toml]\ntriage:\n  questions: {}\n",
        encoding="utf-8",
    )
    conn = make_db(tmp_path / ".state" / "repka.db")
    _seed(conn)
    conn.close()

    at = AppTest.from_file(str(PAGE), default_timeout=60).run()
    assert not at.exception
    assert not at.error
