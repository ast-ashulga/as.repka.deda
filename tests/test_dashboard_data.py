"""Dashboard data layer on a fixture DB. Needs pandas: run with `uv run --group dashboard pytest`."""

import json
from datetime import UTC, datetime, timedelta

import pytest

pd = pytest.importorskip("pandas")

from helpers_digest import add_repo, add_snapshot, add_verdict, make_db  # noqa: E402

from repka import dashboard_data as data  # noqa: E402
from repka.config import DigestConfig  # noqa: E402
from repka.domain.schema import Domain, SearchSpec  # noqa: E402
from repka.models import KeyFile  # noqa: E402
from repka.store import repo  # noqa: E402
from repka.store.metrics import repo_metrics  # noqa: E402

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _domain(domain_id: str) -> Domain:
    search = SearchSpec(keywords=(("mcp",),), topics=(), languages=(), exclude=())
    return Domain(id=domain_id, name=f"Domain {domain_id}", enabled=True,
                  scan_every=timedelta(hours=12), profile="p", search=search)


@pytest.fixture
def conn(tmp_path):
    conn = make_db(tmp_path / "repka.db")
    d = lambda day: datetime(2026, 11, day, 6, 0, tzinfo=UTC)  # noqa: E731
    add_repo(conn, 1, full_name="o/alpha", stars=90, taken_at=datetime(2026, 10, 30, 6, tzinfo=UTC))
    add_snapshot(conn, 1, 100, d(7))
    add_verdict(conn, 1, accepted=True)
    add_repo(conn, 2, full_name="o/beta", stars=10, taken_at=d(7))
    add_verdict(conn, 2, accepted=False)
    add_repo(conn, 3, full_name="o/gamma", stars=5, taken_at=d(7), domain="d2")
    repo.insert_key_file(conn, KeyFile(1, "pyproject.toml", "oid1", None), ["a", "b"], "2026-11-01T00:00:00Z")
    repo.insert_key_file(conn, KeyFile(1, "pyproject.toml", "oid2", None), ["b", "c"], "2026-11-05T00:00:00Z")
    repo.set_domain_scanned(conn, "d1", "2026-11-07T00:00:00Z", "ok", "hash", 43200)
    conn.execute("INSERT INTO runs (kind, started_at, status) VALUES ('run', '2026-11-06T08:00:00Z', 'ok')")
    conn.execute("INSERT INTO runs (kind, started_at, status) VALUES ('run', '2026-11-07T08:00:00Z', 'partial')")
    conn.execute("INSERT INTO digests (slot, local_date, status, item_count) VALUES ('08:30', '2026-11-07', 'sent', 3)")
    conn.commit()
    yield conn
    conn.close()


def test_repos_frame_filters_accepted_and_reports_metrics(conn):
    frame = data.repos_frame(conn, "d1", now=NOW)
    assert sorted(frame["full_name"]) == ["o/alpha", "o/beta"]
    alpha = frame[frame["full_name"] == "o/alpha"].iloc[0]
    assert alpha["stars"] == 100
    assert alpha["stars_delta_7d"] == 10  # baseline 10-30 (90 stars), latest 11-07 (100)
    assert pd.isna(frame[frame["full_name"] == "o/beta"].iloc[0]["stars_delta_7d"])  # one snapshot: n/a

    accepted = data.repos_frame(conn, "d1", accepted_only=True, now=NOW)
    assert list(accepted["full_name"]) == ["o/alpha"]


def test_domains_overview_counts_and_handles_unscanned_domains(conn):
    frame = data.domains_overview(conn, [_domain("d1"), _domain("d2"), _domain("d3")]).set_index("id")
    assert frame.loc["d1", "repos"] == 2 and frame.loc["d1", "accepted"] == 1
    assert frame.loc["d1", "last_scan_status"] == "ok"
    assert frame.loc["d2", "repos"] == 1 and frame.loc["d2", "accepted"] == 0
    assert frame.loc["d3", "repos"] == 0 and pd.isna(frame.loc["d3", "last_scan_at"])


def test_metrics_history_equals_store_metrics_and_keeps_gaps(conn):
    frame = data.metrics_history(conn, [1])
    assert len(frame) == 2  # one row per snapshot, no interpolation
    assert pd.isna(frame.iloc[0]["stars_delta_7d"])
    latest = frame.iloc[-1]
    metrics = repo_metrics(conn, 1, NOW)
    assert latest["stars_delta_7d"] == metrics.stars_delta_7d == 10
    assert latest["velocity"] == metrics.velocity


def test_stack_history_lists_added_and_removed_dependencies(conn):
    frame = data.stack_history(conn, 1)
    changes = [(r["dependency"], r["change"]) for _, r in frame.iterrows()]
    assert changes == [("a", "added"), ("b", "added"), ("c", "added"), ("a", "removed")]


def test_recent_runs_and_digests_newest_first(conn):
    runs = data.recent_runs(conn, 10)
    assert list(runs["status"]) == ["partial", "ok"]
    assert list(data.recent_runs(conn, 1)["status"]) == ["partial"]
    assert list(data.recent_digests(conn, 10)["status"]) == ["sent"]


def test_recent_digests_shows_kind_and_strips_the_slot_prefix(conn):
    conn.execute(
        "INSERT INTO digests (slot, local_date, status, item_count, kind) VALUES ('releases:09:00', '2026-11-07', 'sent', 2, 'releases')"
    )
    conn.commit()
    frame = data.recent_digests(conn, 10)
    assert list(frame["kind"]) == ["releases", "main"]
    assert list(frame["slot"]) == ["09:00", "08:30"]  # no `releases:` prefix


def _add_event(conn, repo_id, kind, payload, detected_at, domain="d1"):
    conn.execute(
        "INSERT INTO events (repo_id, domain_id, kind, payload_json, detected_at) VALUES (?, ?, ?, ?, ?)",
        (repo_id, domain, kind, json.dumps(payload), detected_at),
    )
    conn.commit()


def test_recent_releases_lists_accepted_repos_in_the_window(conn):
    _add_event(conn, 1, "release", {"tag": "v1", "published_at": "2026-11-07T01:00:00Z"}, "2026-11-07T06:00:00Z")
    _add_event(conn, 1, "release", {"tag": "v2", "published_at": "2026-11-07T09:00:00Z"}, "2026-11-07T10:00:00Z")
    _add_event(conn, 2, "release", {"tag": "v9"}, "2026-11-07T06:00:00Z")  # rejected: hidden
    _add_event(conn, 1, "release", {"tag": "old"}, "2026-10-01T00:00:00Z")  # outside the window

    frame = data.recent_releases(conn, [_domain("d1")], timedelta(hours=24), now=NOW)
    assert list(frame["repo"]) == ["o/alpha"]
    assert list(frame["tag"]) == ["v2"]  # newest tag per repo
    assert frame.iloc[0]["stars"] == 100
    assert data.recent_releases(conn, [_domain("d1")], timedelta(hours=1), now=NOW).empty


def test_waiting_items_counts_per_kind_and_follows_the_releases_flag(conn):
    _add_event(conn, 1, "release", {"tag": "v1"}, "2026-11-07T06:00:00Z")
    _add_event(conn, 1, "new_repo", {}, "2026-11-07T06:00:00Z")
    domains = [_domain("d1")]
    columns = ["domain", "new", "found", "release", "total"]

    insights = data.waiting_items(conn, domains, DigestConfig(insights=True), now=NOW)
    assert list(insights.columns) == columns
    row = insights.iloc[0]
    assert (row["new"] + row["found"], row["release"], row["total"]) == (1, 1, 2)

    # releases move to their own message: still counted, from that message's queue
    both = data.waiting_items(conn, domains, DigestConfig(insights=True, releases_message=True), now=NOW)
    assert both.iloc[0]["release"] == 1

    assert data.waiting_items(conn, [_domain("d9")], DigestConfig(insights=True), now=NOW).empty
