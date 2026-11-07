from datetime import UTC, datetime, timedelta

import pytest
from helpers_digest import add_event, add_repo, add_snapshot, add_verdict

from repka.config import DigestConfig
from repka.digest.build import build_digest
from repka.domain.schema import Domain, SearchSpec

from helpers_digest import make_db

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
DOMAIN = Domain(id="d1", name="MCP", profile="p", search=SearchSpec((), (), (), ()))
CFG = DigestConfig(max_items_per_domain=10, new_max_age=timedelta(days=90), star_spike_min_delta=50)


@pytest.fixture
def conn(tmp_path):
    c = make_db(tmp_path / "t.db")
    yield c
    c.close()


def build(conn, cfg=CFG):
    return build_digest(conn, [DOMAIN], NOW, cfg, "08:30", "2026-11-07")


def items(d):
    return [i for s in d.sections for i in (*s.new_repos, *s.releases, *s.movers, *s.stack_changes)]


def test_rejected_events_consumed_untriaged_pending(conn):
    add_repo(conn, 1)
    add_repo(conn, 2)
    add_repo(conn, 3)
    add_verdict(conn, 1, True)
    add_verdict(conn, 2, False)
    e1, e2, e3 = (add_event(conn, r, "new_repo") for r in (1, 2, 3))
    d = build(conn)
    assert [i.repo_id for i in items(d)] == [1]
    assert d.shown_event_ids == [e1]
    assert d.consumed_event_ids == [e2]
    assert e3 not in d.shown_event_ids + d.consumed_event_ids  # no verdict yet: appears after triage


def test_latest_verdict_decides(conn):
    add_repo(conn, 1)
    add_verdict(conn, 1, False, model="old", decided="2026-11-01T00:00:00Z")
    add_verdict(conn, 1, True, model="new", decided="2026-11-05T00:00:00Z")
    add_event(conn, 1, "new_repo")
    assert [i.repo_id for i in items(build(conn))] == [1]


def test_bare_key_file_change_consumed_stack_change_shown(conn):
    add_repo(conn, 1)
    add_repo(conn, 2)
    add_verdict(conn, 1, True)
    add_verdict(conn, 2, True)
    k1 = add_event(conn, 1, "key_file_changed", {"path": "package.json"})  # bare
    k2 = add_event(conn, 2, "key_file_changed", {"path": "pyproject.toml"})
    s2 = add_event(
        conn,
        2,
        "stack_changed",
        {"path": "pyproject.toml", "added": ["fastmcp"], "removed": ["mcp"]},
    )
    d = build(conn)
    [stack] = items(d)
    assert (stack.repo_id, stack.line_kind) == (2, "stack")
    assert stack.text_fields["changes"] == [{"path": "pyproject.toml", "added": ["fastmcp"], "removed": ["mcp"]}]
    assert d.shown_event_ids == [s2]
    assert sorted(d.consumed_event_ids) == [k1, k2]


def test_cap_and_more_counter(conn):
    # priority new -> releases -> movers -> stack; cap 3 of 1 new + 2 releases + 1 stack
    for r in (1, 2, 3, 4):
        add_repo(conn, r, stars=100 * r)
        add_verdict(conn, r, True)
    n = add_event(conn, 1, "new_repo")
    r2 = add_event(conn, 2, "release", {"tag": "v1"})
    r3 = add_event(conn, 3, "release", {"tag": "v2"})
    st = add_event(conn, 4, "stack_changed", {"path": "p", "added": ["a"], "removed": []})
    d = build(conn, DigestConfig(max_items_per_domain=3))
    [section] = d.sections
    assert [i.repo_id for i in section.new_repos] == [1]
    assert [i.repo_id for i in section.releases] == [3, 2]  # stars desc
    assert section.stack_changes == []
    assert section.more == 1
    assert d.item_count == 3
    assert sorted(d.shown_event_ids) == sorted([n, r2, r3])
    assert d.consumed_event_ids == [st]


def test_mover_threshold_rf4_and_no_repeat_within_24h(conn):
    t_old = NOW - timedelta(days=2)
    add_repo(conn, 1, stars=500, taken_at=t_old)  # baseline 2 days ago
    add_snapshot(conn, 1, 600, NOW - timedelta(hours=1))  # +100
    add_repo(conn, 2, stars=40, taken_at=t_old)
    add_snapshot(conn, 2, 60, NOW - timedelta(hours=1))  # +20: below threshold
    add_repo(conn, 3, stars=9000, taken_at=NOW - timedelta(hours=1))  # first scan: no baseline (RF4)
    for r in (1, 2, 3):
        add_verdict(conn, r, True)
    d = build(conn)
    assert [(i.repo_id, i.line_kind, i.text_fields["stars_delta_1d"]) for i in items(d)] == [(1, "mover", 100)]
    assert d.shown_event_ids == []  # movers are not events

    def record(status, sent_at):
        cur = conn.execute(
            "INSERT INTO digests (slot, local_date, status, sent_at) VALUES ('s', ?, ?, ?)",
            (sent_at, status, sent_at),
        )
        conn.execute("INSERT INTO digest_items VALUES (?, 1, 'mover')", (cur.lastrowid,))

    record("sent", "2026-11-06T13:00:00Z")  # 23 h ago: suppressed
    assert items(build(conn)) == []
    conn.execute("DELETE FROM digest_items")
    conn.execute("DELETE FROM digests")
    record("sent", "2026-11-06T11:00:00Z")  # 25 h ago: can be shown again
    assert [i.repo_id for i in items(build(conn))] == [1]


def test_new_repo_ranking(conn):
    specs = {
        1: (2, 0.5, 100),
        2: (4, 0.5, 10),
        3: (4, 0.9, 10),
        4: (4, 0.9, 500),
    }  # substance, relevant, stars
    for r, (sub, rel, stars) in specs.items():
        add_repo(conn, r, stars=stars)
        add_verdict(conn, r, True, substance=sub, relevant=rel)
        add_event(conn, r, "new_repo")
    [section] = build(conn).sections
    assert [i.repo_id for i in section.new_repos] == [4, 3, 2, 1]


def test_old_repo_found_later_is_marked_found_not_new(conn):  # RF8
    add_repo(conn, 1, created="2026-10-01T00:00:00Z")
    add_repo(conn, 2, created="2019-05-01T00:00:00Z")
    for r in (1, 2):
        add_verdict(conn, r, True)
        add_event(conn, r, "new_repo")
    [section] = build(conn).sections
    by_id = {i.repo_id: i for i in section.new_repos}
    assert by_id[1].line_kind == "new"
    assert by_id[2].line_kind == "found"
    assert by_id[2].text_fields["since"] == 2019


def test_empty_when_nothing_new(conn):
    add_repo(conn, 1)
    add_verdict(conn, 1, True)
    d = build(conn)
    assert d.item_count == 0 and d.sections == []


def test_builder_writes_nothing(conn):
    add_repo(conn, 1)
    add_repo(conn, 2)
    add_verdict(conn, 1, True)
    add_verdict(conn, 2, False)
    add_event(conn, 1, "new_repo")
    add_event(conn, 2, "new_repo")
    conn.commit()
    before = conn.total_changes
    build(conn)
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM events WHERE digest_id IS NULL").fetchone()[0] == 2
