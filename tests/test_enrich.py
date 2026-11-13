import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from repka.config import CollectConfig, GithubConfig
from repka.github.client import GitHubClient, GitHubError, RateLimited
from repka.github.enrich import build_enrich_query, enrich_repos, snapshot_from_node
from repka.models import KeyFile, RepoHit, Snapshot
from repka.store import repo
from repka.store.db import connect, migrate

FIX = Path(__file__).parent / "fixtures" / "github"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
NODES = json.loads((FIX / "graphql_nodes.json").read_text())["nodes"]
NULL_NODE = json.loads((FIX / "graphql_null_node.json").read_text())
BLOB_TEXT = json.loads((FIX / "graphql_blob_texts.json").read_text())["nodes"][0]["text"]
SERIES = json.loads((FIX / "participation.json").read_text())["all"]  # last 12 weeks: 9 non-zero
CFG = CollectConfig(enrich_batch=50, slow_metrics_every=timedelta(days=7))
KEY_FILES = ["pyproject.toml", "package.json"]


def node(repo_id: int, base: int = 0, **override) -> dict:
    n = copy.deepcopy(NODES[base])
    n.update(id=f"R_{repo_id}", databaseId=repo_id, **override)
    return n


class FakeClient:
    """Answers GraphQL from a node-id map and records every call."""

    def __init__(self, *nodes: dict, texts: dict[str, str | None] | None = None):
        self.nodes = {n["id"]: n for n in nodes}
        self.texts = texts if texts is not None else {"B_0": BLOB_TEXT}
        self.graphql_calls: list[str] = []
        self.contributors_calls: list[str] = []
        self.participation_calls: list[str] = []
        self.contributors = 42
        self.series: list[int] | None = SERIES
        self.override: dict | None = None

    def graphql(self, query, variables):
        self.graphql_calls.append(query)
        if self.override is not None:
            return self.override
        if "stargazerCount" in query:
            return {"nodes": [self.nodes.get(i) for i in variables["ids"]]}
        return {"nodes": [{"text": self.texts.get(i)} for i in variables["ids"]]}

    def contributors_count(self, full_name):
        self.contributors_calls.append(full_name)
        return self.contributors

    def participation(self, full_name):
        self.participation_calls.append(full_name)
        return self.series


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    migrate(c)
    yield c
    c.close()


def add_repo(conn, repo_id: int) -> None:
    repo.upsert_repo(
        conn,
        RepoHit(repo_id, f"R_{repo_id}", f"octo/repo{repo_id}", "https://github.com/x", None, None, (), 1, 0,
                False, NOW, NOW, False, None),
        "2026-11-01T00:00:00Z",
    )


def events(conn, repo_id: int | None = None) -> list[tuple[int, str, dict]]:
    rows = conn.execute("SELECT repo_id, kind, payload_json FROM events ORDER BY id").fetchall()
    return [(r["repo_id"], r["kind"], json.loads(r["payload_json"])) for r in rows if repo_id in (None, r["repo_id"])]


def old_snapshot(repo_id: int, taken_at: datetime, tag: str | None = None) -> Snapshot:
    return Snapshot(repo_id, taken_at, 1, 1, 0, 0, None, 0, 0, None, 0, tag, None)


def run(client, conn, ids, now=NOW, key_files=(), slow_for=frozenset(), cfg=CFG):
    return enrich_repos(client, conn, "d1", ids, key_files, now, cfg, set(slow_for))


def test_build_query_has_alias_per_key_file():
    query, aliases = build_enrich_query(["pyproject.toml", "go.mod"])
    assert aliases == {"kf0": "pyproject.toml", "kf1": "go.mod"}
    assert 'kf0: object(expression: "HEAD:pyproject.toml")' in query
    assert 'kf1: object(expression: "HEAD:go.mod")' in query
    assert "$ids: [ID!]!" in query and "$since7" in query and "$since30" in query


def test_snapshot_from_node():
    prev = old_snapshot(100, NOW - timedelta(days=1))
    prev = Snapshot(**{**prev.__dict__, "contributors": 7, "active_weeks_12": 4})
    s = snapshot_from_node(NODES[0], NOW, prev)
    assert (s.repo_id, s.stars, s.forks, s.open_issues, s.watchers) == (100, 1200, 80, 14, 33)
    assert (s.commits_7d, s.commits_30d, s.releases_90d) == (5, 21, 2)  # v1.0.0 is older than 90 days
    assert s.latest_release_tag == "v1.2.0"
    assert s.latest_release_at == datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
    assert (s.contributors, s.active_weeks_12) == (7, 4)  # carried over from prev
    assert snapshot_from_node(NODES[0], NOW, None).contributors is None


def test_empty_repo_commits_zero():
    s = snapshot_from_node(NODES[1], NOW, None)
    assert (s.commits_7d, s.commits_30d, s.releases_90d) == (0, 0, 0)
    assert s.latest_release_tag is None and s.latest_release_at is None


def test_null_node_marks_repo_gone(conn):
    add_repo(conn, 100)
    repo.insert_snapshot(conn, old_snapshot(100, NOW - timedelta(days=1)))
    client = FakeClient()
    client.override = NULL_NODE
    stats = run(client, conn, [100])
    assert stats.gone == 1 and stats.repos == 0
    assert conn.execute("SELECT status FROM repos WHERE repo_id = 100").fetchone()["status"] == "gone"
    assert repo.latest_snapshot(conn, 100) is not None  # history kept


def test_release_event_on_new_tag(conn):
    add_repo(conn, 100)
    repo.insert_snapshot(conn, old_snapshot(100, NOW - timedelta(days=1), tag="v1.1.0"))
    client = FakeClient(node(100))
    stats = run(client, conn, [100])
    assert events(conn) == [
        (100, "release", {
            "tag": "v1.2.0",
            "name": "Release 1.2.0",
            "published_at": "2026-11-05T10:00:00Z",
            "url": "https://github.com/octo/cat/releases/tag/v1.2.0",
        })
    ]
    assert stats.events == 1
    run(client, conn, [100], now=NOW + timedelta(hours=1))  # same tag again: nothing new
    assert len(events(conn)) == 1


def _with_prerelease_on_top(base: int = 0) -> dict:
    releases = copy.deepcopy(NODES[base]["releases"])
    pre = {
        "tagName": "v1.3.0-nightly.1",
        "name": "nightly",
        "publishedAt": "2026-11-06T10:00:00Z",
        "createdAt": "2026-11-06T09:50:00Z",
        "url": "https://github.com/octo/cat/releases/tag/v1.3.0-nightly.1",
        "isPrerelease": True,
        "isDraft": False,
    }
    releases["nodes"].insert(0, pre)
    return releases


def test_snapshot_latest_release_skips_prereleases_and_drafts():
    releases = _with_prerelease_on_top()
    releases["nodes"].insert(0, {**releases["nodes"][0], "tagName": "v9-draft", "isPrerelease": False, "isDraft": True})
    s = snapshot_from_node(node(100, releases=releases), NOW, None)
    assert s.latest_release_tag == "v1.2.0"
    assert s.releases_90d == 4  # cadence still counts every release in the window
    only_pre = {"nodes": [releases["nodes"][1]]}
    s = snapshot_from_node(node(100, releases=only_pre), NOW, None)
    assert s.latest_release_tag is None and s.latest_release_at is None


def test_prerelease_emits_no_release_event_but_the_stable_one_does(conn):
    add_repo(conn, 100)
    repo.insert_snapshot(conn, old_snapshot(100, NOW - timedelta(days=1), tag="v1.1.0"))
    client = FakeClient(node(100, releases=_with_prerelease_on_top()))
    run(client, conn, [100])
    assert [e[2]["tag"] for e in events(conn)] == ["v1.2.0"]  # not the nightly


def test_no_release_event_when_stable_release_is_older_than_the_last_seen_prerelease(conn):
    add_repo(conn, 100)
    prev = old_snapshot(100, NOW - timedelta(days=1), tag="v1.3.0-nightly.0")  # recorded before the filter existed
    repo.insert_snapshot(conn, Snapshot(**{**prev.__dict__, "latest_release_at": datetime(2026, 11, 6, 8, 0, tzinfo=UTC)}))
    client = FakeClient(node(100, releases=_with_prerelease_on_top()))
    run(client, conn, [100])
    assert events(conn) == []  # v1.2.0 (11-05) predates what was already seen (11-06)


def test_no_release_event_for_old_release_on_first_sighting(conn):
    add_repo(conn, 100)
    add_repo(conn, 300)
    old = copy.deepcopy(NODES[0]["releases"])
    for r in old["nodes"]:
        r["publishedAt"] = r["createdAt"] = "2026-10-01T10:00:00Z"
    client = FakeClient(node(100, releases=old), node(300))
    run(client, conn, [100, 300])
    assert [(rid, kind) for rid, kind, _ in events(conn)] == [(300, "release")]  # only the 2-day-old one


def test_baseline_scan_emits_no_release_event_for_a_first_sighting(conn):  # silent baseline
    add_repo(conn, 100)
    add_repo(conn, 300)
    repo.insert_snapshot(conn, old_snapshot(300, NOW - timedelta(days=1), tag="v1.1.0"))  # known from another domain
    client = FakeClient(node(100), node(300))
    stats = enrich_repos(client, conn, "d1", [100, 300], (), NOW, CFG, set(), baseline=True)
    assert [(rid, kind) for rid, kind, _ in events(conn)] == [(300, "release")]  # a real new tag still counts
    assert stats.repos == 2 and repo.latest_snapshot(conn, 100) is not None  # the snapshot itself is written


def test_key_file_change_emits_change_and_stack_events(conn):
    add_repo(conn, 100)
    repo.insert_snapshot(conn, old_snapshot(100, NOW - timedelta(days=1), tag="v1.2.0"))  # no release event
    repo.insert_key_file(conn, KeyFile(100, "pyproject.toml", "oid-old", None), ["httpx", "requests"], "2026-11-01T00:00:00Z")
    client = FakeClient(node(100))
    run(client, conn, [100], key_files=KEY_FILES)
    assert events(conn) == [
        (100, "key_file_changed", {"path": "pyproject.toml", "old_oid": "oid-old", "new_oid": "oid-pyproject-1"}),
        (100, "stack_changed", {"path": "pyproject.toml", "added": ["pydantic"], "removed": ["requests"]}),
    ]
    assert repo.latest_key_file(conn, 100, "pyproject.toml") == ("oid-pyproject-1", ["httpx", "pydantic"])
    assert repo.latest_key_file(conn, 100, "package.json") is None  # missing file: nothing stored
    assert len(client.graphql_calls) == 2  # one main query + one for the changed blob texts
    run(client, conn, [100], now=NOW + timedelta(hours=1), key_files=KEY_FILES)  # unchanged oid
    assert len(events(conn)) == 2 and len(client.graphql_calls) == 3  # no extra text query


def test_first_key_file_sighting_emits_nothing(conn):
    add_repo(conn, 100)
    repo.insert_snapshot(conn, old_snapshot(100, NOW - timedelta(days=1), tag="v1.2.0"))
    run(FakeClient(node(100)), conn, [100], key_files=KEY_FILES)
    assert events(conn) == []
    assert repo.latest_key_file(conn, 100, "pyproject.toml") == ("oid-pyproject-1", ["httpx", "pydantic"])


def test_poisonous_node_splits_the_batch_and_is_skipped(conn):
    """GitHub answers a batch holding R_300 with an empty 200 (as it does when the query runs too long).
    The batch is retried as halves; only R_300 is left without a snapshot and it is counted."""
    ids = [100, 200, 300, 400, 500, 600]
    for rid in ids:
        add_repo(conn, rid)
    nodes = {f"R_{rid}": node(rid) for rid in ids}
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked = body["variables"]["ids"]
        sizes.append(len(asked))
        if "R_300" in asked:
            return httpx.Response(200, content=b"")  # -> GitHubError: non-JSON body
        return httpx.Response(200, json={"data": {"nodes": [nodes[i] for i in asked]}})

    client = GitHubClient("tok", GithubConfig(), transport=httpx.MockTransport(handler), max_server_retries=0)
    stats = run(client, conn, ids, cfg=CollectConfig(enrich_batch=6))

    assert stats.repos == 5 and stats.skipped == 1
    assert sizes == [6, 3, 1, 2, 1, 1, 3]  # 6 x, 3 x, [100] ok, 2 x, [200] ok, [300] x, [400 500 600] ok
    assert repo.latest_snapshot(conn, 300) is None  # left for the next run
    assert all(repo.latest_snapshot(conn, rid) for rid in ids if rid != 300)


def test_failing_blob_text_query_is_retried_as_halves_not_fatal(conn):
    """The key-file text query shares the skip logic of the node query: one 5xx must not lose the batch."""

    class FlakyTexts(FakeClient):
        def __init__(self, *nodes, **kw):
            super().__init__(*nodes, **kw)
            self.failed = False

        def graphql(self, query, variables):
            if "stargazerCount" not in query and not self.failed:
                self.failed = True
                self.graphql_calls.append(query)
                raise GitHubError("GraphQL returned no data: 502")
            return super().graphql(query, variables)

    for rid in (100, 200):
        add_repo(conn, rid)
    client = FlakyTexts(node(100), node(200, base=1))
    stats = run(client, conn, [100, 200], key_files=KEY_FILES)
    assert stats.repos == 2 and stats.skipped == 0
    assert all(repo.latest_snapshot(conn, rid) for rid in (100, 200))


def test_batches_commit_and_call_count(conn):
    for rid in (100, 200, 300):
        add_repo(conn, rid)
    client = FakeClient(node(100), node(200, base=1), node(300, base=1))
    stats = run(client, conn, [100, 200, 300], cfg=CollectConfig(enrich_batch=2))
    assert stats.repos == 3 and len(client.graphql_calls) == 2  # ceil(3 / 2) batches, no key files changed
    assert all(repo.latest_snapshot(conn, rid) for rid in (100, 200, 300))


def test_slow_metrics_only_when_due(conn):
    add_repo(conn, 100)
    client = FakeClient(node(100))
    stats = run(client, conn, [100], slow_for={100})
    assert (client.contributors_calls, client.participation_calls) == (["octo/repo100"], ["octo/repo100"])
    assert stats.slow_refreshed == 1
    s = repo.latest_snapshot(conn, 100)
    assert (s.contributors, s.active_weeks_12) == (42, 9)
    run(client, conn, [100], now=NOW + timedelta(days=1), slow_for={100})  # fresh: no calls, values carried
    assert len(client.contributors_calls) == len(client.participation_calls) == 1
    s = repo.latest_snapshot(conn, 100)
    assert (s.contributors, s.active_weeks_12) == (42, 9)
    run(client, conn, [100], now=NOW + timedelta(days=8), slow_for={100})  # stale: refreshed again
    assert len(client.contributors_calls) == len(client.participation_calls) == 2


def test_slow_metrics_only_for_accepted(conn):
    add_repo(conn, 100)
    add_repo(conn, 200)
    client = FakeClient(node(100), node(200, base=1))
    run(client, conn, [100, 200], slow_for=set())
    assert client.contributors_calls == [] and client.participation_calls == []
    run(client, conn, [100, 200], now=NOW + timedelta(hours=1), slow_for={100})
    assert client.contributors_calls == ["octo/repo100"] and client.participation_calls == ["octo/repo100"]
    assert repo.latest_snapshot(conn, 200).contributors is None


def test_participation_202_stores_null_and_retries_next_time(conn):
    add_repo(conn, 100)
    client = FakeClient(node(100))
    client.series = None  # GitHub answered 202
    run(client, conn, [100], slow_for={100})
    s = repo.latest_snapshot(conn, 100)
    assert (s.contributors, s.active_weeks_12) == (42, None)
    client.series = SERIES
    run(client, conn, [100], now=NOW + timedelta(hours=1), slow_for={100})
    assert len(client.participation_calls) == 2  # retried at the next run
    assert len(client.contributors_calls) == 1  # contributors were fresh
    assert repo.latest_snapshot(conn, 100).active_weeks_12 == 9


def test_unexpected_error_skips_repo_but_rate_limit_stops_run(conn):
    add_repo(conn, 100)
    add_repo(conn, 300)
    broken = node(100)
    del broken["stargazerCount"]  # malformed node: this repo is skipped, the run goes on
    stats = run(FakeClient(broken, node(300)), conn, [100, 300])
    assert stats.repos == 1 and repo.latest_snapshot(conn, 100) is None and repo.latest_snapshot(conn, 300)

    class Limited(FakeClient):
        def contributors_count(self, full_name):
            raise RateLimited("slow down", None)

    with pytest.raises(RateLimited):
        run(Limited(node(100)), conn, [100], now=NOW + timedelta(hours=1), slow_for={100})
    assert repo.latest_snapshot(conn, 100) is None  # the interrupted repo left nothing half-written


def test_events_go_to_every_domain_sharing_the_repo(conn):
    add_repo(conn, 100)
    for d in ("d1", "d2"):
        repo.link_domain_repo(conn, d, 100, "q", "2026-11-01T00:00:00Z")
    repo.insert_snapshot(conn, old_snapshot(100, NOW - timedelta(days=1), tag="v1.1.0"))
    repo.insert_key_file(conn, KeyFile(100, "pyproject.toml", "oid-old", None), ["httpx"], "2026-11-01T00:00:00Z")
    enrich_repos(FakeClient(node(100)), conn, "d1", [100], KEY_FILES, NOW, CFG, set(),
                 key_files_by_domain={"d1": KEY_FILES, "d2": ["go.mod"]})  # fmt: skip
    got = conn.execute("SELECT domain_id, kind FROM events ORDER BY id").fetchall()
    assert {(r["domain_id"], r["kind"]) for r in got} == {
        ("d1", "release"), ("d2", "release"), ("d1", "key_file_changed"), ("d1", "stack_changed"),
    }  # d2 does not track pyproject.toml
