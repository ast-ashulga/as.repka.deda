import json
import re
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from repka.domain.matrix import Query, Window, build_queries
from repka.domain.schema import load_domain
from repka.github.client import RateLimited, SearchPage
from repka.github.search import Slice, collect_query, collect_search, hit_from_item
from repka.store import repo
from repka.store.db import connect, migrate

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "github"
NOW = "2026-11-08T08:00:00Z"
QUERY = Query(key="q1", q="topic:mcp archived:false", term="mcp", language=None)
QUERY_B = Query(key="q2", q="mcp in:name archived:false", term="mcp", language=None)
SLICE_RE = re.compile(r"pushed:(\d{4}-\d\d-\d\d)\.\.(\d{4}-\d\d-\d\d)")


def days_of(qualifier: str) -> int:
    start, end = (date.fromisoformat(g) for g in SLICE_RE.fullmatch(qualifier).groups())
    return (end - start).days + 1


def window(days: int) -> Window:
    return Window("pushed", date(2026, 11, 1), date(2026, 11, 1 + days - 1))


class FakeSearchClient:
    """Answers search_repos from `total(days)`: that many results per slice, ranked 1..total."""

    def __init__(self, total, *, fail_on=None, incomplete_once=()):
        self.total = total
        self.fail_on = fail_on  # (qualifier, page): raise RateLimited when that request is made
        self.incomplete_once = set(incomplete_once)  # (qualifier, page) answered half-empty once at per_page 100
        self.calls: list[tuple[str, int, int]] = []

    def qualifiers(self) -> set[str]:
        return {c[0] for c in self.calls}

    def search_repos(self, q, page=1, per_page=None):
        start, end = (date.fromisoformat(g) for g in SLICE_RE.search(q).groups())
        qualifier = SLICE_RE.search(q).group(0)
        self.calls.append((qualifier, page, per_page))
        if self.fail_on == (qualifier, page):
            raise RateLimited("limited", None)
        total = self.total((end - start).days + 1)
        offset = (page - 1) * per_page
        assert offset < 1000, "search never serves results beyond the first 1000"
        ranks = range(offset + 1, min(offset + per_page, total, 1000) + 1)
        items = [self._item(start, r) for r in ranks]
        if (qualifier, page) in self.incomplete_once and per_page == 100:
            self.incomplete_once.discard((qualifier, page))
            return SearchPage(total, True, items[: len(items) // 2])
        return SearchPage(total, False, items)

    @staticmethod
    def _item(start, rank):
        rid = start.toordinal() * 100_000 + rank
        return {
            "id": rid,
            "node_id": f"R_{rid}",
            "full_name": f"o/r{rid}",
            "html_url": f"https://github.com/o/r{rid}",
            "stargazers_count": 1000 - rank,
            "forks_count": 0,
            "created_at": "2026-06-01T00:00:00Z",
            "pushed_at": "2026-11-01T00:00:00Z",
        }


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "t.db"
    c = connect(path)
    migrate(c)
    c.close()
    return path


@pytest.fixture
def conn(db_file):
    c = connect(db_file)
    yield c
    c.close()


def count(conn, table, where=""):
    return conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


def run(client, conn, w, budget=10_000, emit_new=True, query=QUERY):
    return collect_query(client, conn, "d1", query, w, NOW, budget, emit_new)


def test_hit_from_item_maps_fields():
    item = json.loads((FIXTURES / "search_item.json").read_text())
    hit = hit_from_item(item)
    assert hit.repo_id == item["id"]
    assert (hit.node_id, hit.full_name, hit.html_url) == (item["node_id"], item["full_name"], item["html_url"])
    assert hit.stars == item["stargazers_count"] and hit.forks == item["forks_count"]
    assert hit.topics == tuple(item["topics"]) and hit.license == "MIT"
    assert hit.created_at.isoformat() == "2026-04-14T00:51:32+00:00" and hit.is_fork is False


def test_slice_halves_and_qualifier():
    s = Slice("pushed", date(2026, 1, 1), date(2026, 1, 8))
    a, b = s.halves()
    assert (s.days(), a.days(), b.days()) == (8, 4, 4)
    assert b.start == date(2026, 1, 5) and s.qualifier() == "pushed:2026-01-01..2026-01-08"
    one, two = Slice("pushed", date(2026, 1, 1), date(2026, 1, 2)).halves()
    assert one.days() == two.days() == 1


def test_pages_until_short_page(conn):
    client = FakeSearchClient(lambda days: 250)
    stats, seen = run(client, conn, window(1))
    assert [c[1] for c in client.calls] == [1, 2, 3]
    assert (stats.pages, stats.hits, stats.slices, stats.capped) == (3, 250, 1, 0)
    assert len(seen) == 250 and count(conn, "repos") == 250
    assert repo.cursor_done(conn, "d1", "q1", "pushed:2026-11-01..2026-11-01")


def test_bisection_splits_when_over_1000(conn):
    client = FakeSearchClient(lambda days: days * 313)  # 8 days -> 2504 results
    stats, _ = run(client, conn, window(8))
    paged = {c[0] for c in client.calls if c[1] > 1}  # slices that were actually read page by page
    assert paged and all(client.total(days_of(q)) <= 1000 for q in paged)
    assert stats.hits == 8 * 313 and stats.capped == 0 and count(conn, "repos") == 8 * 313


def test_bisection_stops_at_one_day_and_caps(conn):  # RF2
    client = FakeSearchClient(lambda days: 5000)
    stats, _ = run(client, conn, window(1))
    assert stats.hits == 1000 and stats.capped == 1 and stats.pages == 10
    assert client.qualifiers() == {"pushed:2026-11-01..2026-11-01"}
    row = conn.execute("SELECT capped, total_count FROM search_cursor").fetchone()
    assert (row["capped"], row["total_count"]) == (1, 5000)
    # a multi-day window never recurses below one day either
    deeper = FakeSearchClient(lambda days: 5000)
    run(deeper, conn, window(2), query=QUERY_B)
    assert all(SLICE_RE.fullmatch(q) for q in deeper.qualifiers())
    assert {q for q in deeper.qualifiers()} == {
        "pushed:2026-11-01..2026-11-02",
        "pushed:2026-11-01..2026-11-01",
        "pushed:2026-11-02..2026-11-02",
    }


def test_resume_skips_completed_slices(db_file):  # RF1
    leaf = lambda a, b: f"pushed:2026-11-0{a}..2026-11-0{b}"
    l1, l2, l3, l4 = leaf(1, 2), leaf(3, 4), leaf(5, 6), leaf(7, 8)
    first = FakeSearchClient(lambda days: days * 313, fail_on=(l3, 2))
    c1 = connect(db_file)
    with pytest.raises(RateLimited):
        run(first, c1, window(8))
    c1.rollback()  # whatever was not committed is gone
    c1.close()

    c2 = connect(db_file)
    assert repo.cursor_done(c2, "d1", "q1", l1) and repo.cursor_done(c2, "d1", "q1", l2)
    assert not repo.cursor_done(c2, "d1", "q1", l3)
    second = FakeSearchClient(lambda days: days * 313)
    run(second, c2, window(8))
    # slices 1-2 (and their finished parent) are never requested again; 3..n are
    assert second.qualifiers() == {"pushed:2026-11-01..2026-11-08", "pushed:2026-11-05..2026-11-08", l3, l4}
    assert count(c2, "repos") == 8 * 313
    c2.close()


def test_rate_limited_keeps_committed_pages(db_file):  # RF1
    client = FakeSearchClient(lambda days: 350, fail_on=("pushed:2026-11-01..2026-11-01", 3))
    c1 = connect(db_file)
    with pytest.raises(RateLimited):
        run(client, c1, window(1))
    c1.rollback()
    c1.close()
    c2 = connect(db_file)
    assert count(c2, "repos") == 200 and count(c2, "domain_repos") == 200
    assert count(c2, "search_cursor") == 0
    c2.close()


def test_new_repo_event_only_on_first_sighting(conn):
    client = FakeSearchClient(lambda days: 3)
    run(client, conn, window(1), query=QUERY)
    run(client, conn, window(1), query=QUERY_B)  # same repos through another query
    events = conn.execute("SELECT kind, payload_json, domain_id FROM events").fetchall()
    assert len(events) == 3 and {e["kind"] for e in events} == {"new_repo"}
    assert json.loads(events[0]["payload_json"]) == {"created_at": "2026-06-01T00:00:00Z"}
    assert json.loads(conn.execute("SELECT query_keys_json FROM domain_repos").fetchone()[0]) == ["q1", "q2"]


def test_first_scan_is_silent_baseline(conn):  # RF8
    run(FakeSearchClient(lambda days: 5), conn, window(1), emit_new=False)
    assert count(conn, "repos") == 5 and count(conn, "domain_repos") == 5
    assert count(conn, "events") == 0


def test_incomplete_results_retried_with_per_page_50(conn):
    q = "pushed:2026-11-01..2026-11-01"
    client = FakeSearchClient(lambda days: 100, incomplete_once=[(q, 1)])
    stats, _ = run(client, conn, window(1))
    assert client.calls[:3] == [(q, 1, 100), (q, 1, 50), (q, 2, 50)]
    assert stats.incomplete == 1 and stats.hits == 100 and count(conn, "repos") == 100


def test_budget_caps_hits(conn):
    client = FakeSearchClient(lambda days: 1000)
    stats, _ = run(client, conn, window(1), budget=150)
    assert stats.hits == 150 and stats.pages == 2 and count(conn, "repos") == 150
    assert count(conn, "search_cursor") == 0  # unfinished slice is re-read next run


def test_collect_search_sums_queries_within_max_repos(conn):
    domain = load_domain(REPO / "domains" / "example-mcp.yaml")
    n = len(build_queries(domain))
    stats, seen = collect_search(FakeSearchClient(lambda days: 2), conn, domain, window(1), NOW, 10_000, False)
    assert stats.slices == n and stats.hits == 2 and len(seen) == 2  # the same 2 repos through every query: counted once
    other = replace(domain, id="d2")
    capped, _ = collect_search(FakeSearchClient(lambda days: 2), conn, other, window(1), NOW, 2, False)
    assert (capped.hits, capped.slices) == (2, 1)  # the budget is used up by the first query
