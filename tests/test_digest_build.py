from datetime import UTC, datetime, timedelta

import pytest
from helpers_digest import add_event, add_repo, add_snapshot, add_verdict

from repka.config import DigestConfig
from repka.digest.build import MAX_FACETS, build_digest, facet_tags, pending_overflow
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


def test_mover_not_repeated_within_24h(conn):
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


INSIGHTS = DigestConfig(
    max_items_per_domain=10, new_max_age=timedelta(days=90), star_spike_min_delta=50, insights=True
)


def _noul(p):
    return {"type": "noul", "p": p, "pred": True, "conf": p}


def test_facet_tags_filters_sorts_and_caps():
    answers = {
        "relevant": _noul(0.99),
        "substance": {"type": "score", "pred": 4, "score": 4.0, "conf": 0.6, "probs": {}},
        "kind": {"type": "choice", "pred": "tool", "conf": 0.7, "probs": {}},
        "e": _noul(0.60),
        "d": _noul(0.72),
        "c": _noul(0.75),
        "b": _noul(0.80),
        "a": _noul(0.95),
        "is_junk": _noul(0.99),
    }
    assert MAX_FACETS == 3
    assert facet_tags(answers, 0.7) == [("a", 0.95), ("b", 0.8), ("c", 0.75)]


def test_facet_tags_ties_sorted_by_id_and_ignores_malformed():
    answers = {"z": _noul(0.9), "y": _noul(0.9), "bad": {"type": "noul"}, "s": "x", "c": {"type": "choice", "p": 0.99}}
    assert facet_tags(answers, 0.7) == [("y", 0.9), ("z", 0.9)]


def test_facet_tags_empty_without_noul_extras():
    base = {
        "relevant": _noul(0.99),
        "substance": {"type": "score", "pred": 4, "score": 4.0, "conf": 0.6, "probs": {}},
        "kind": {"type": "choice", "pred": "tool", "conf": 0.7, "probs": {}},
        "is_junk": _noul(0.99),
    }
    assert facet_tags(base, 0.7) == []
    assert facet_tags({}, 0.7) == []


def test_insights_items_carry_facets(conn):
    add_repo(conn, 1, created="2026-10-01T00:00:00Z")  # new
    add_repo(conn, 2, created="2019-05-01T00:00:00Z")  # found
    add_verdict(conn, 1, True, extra_answers={"self_modifies": _noul(0.94)})
    add_verdict(conn, 2, True)
    for r in (1, 2):
        add_event(conn, r, "new_repo")
    [section] = build(conn, INSIGHTS).sections
    by_id = {i.repo_id: i for i in section.new_repos}
    assert by_id[1].text_fields["facets"] == [("self_modifies", 0.94)]
    assert by_id[2].text_fields["facets"] == []


def test_insights_off_adds_no_facets_field(conn):
    add_repo(conn, 1, created="2026-10-01T00:00:00Z")
    add_verdict(conn, 1, True, extra_answers={"self_modifies": _noul(0.94)})
    add_event(conn, 1, "new_repo")
    [section] = build(conn).sections
    assert "facets" not in section.new_repos[0].text_fields


def test_mover_item_enriched_with_description_and_kind(conn):
    t_old = NOW - timedelta(days=2)
    add_repo(conn, 1, stars=500, taken_at=t_old, description="spiky tool")
    add_snapshot(conn, 1, 600, NOW - timedelta(hours=1))
    add_repo(conn, 2, stars=500, taken_at=t_old, description="no facets")
    add_snapshot(conn, 2, 600, NOW - timedelta(hours=1))
    add_verdict(conn, 1, True, substance=4, kind="framework", extra_answers={"self_modifies": _noul(0.94)})
    add_verdict(conn, 2, True)
    off = {i.repo_id: i for i in items(build(conn))}
    assert set(off[1].text_fields) == {"stars_delta_1d", "velocity", "stars"}
    on = {i.repo_id: i for i in items(build(conn, INSIGHTS))}
    f = on[1].text_fields
    assert (f["description"], f["kind"], f["level"], f["stars"]) == ("spiky tool", "framework", 4, 600)
    assert f["facets"] == [("self_modifies", 0.94)]
    assert f["stars_delta_1d"] == 100
    assert on[2].text_fields["facets"] == []


def _seed_full_digest(conn):
    """One of everything plus overflow, rejected and untriaged repos; returns named event ids."""
    ev = {}
    t_old = NOW - timedelta(days=2)
    for r in range(1, 10):  # nine new/found repos
        created = "2019-05-01T00:00:00Z" if r == 9 else "2026-10-01T00:00:00Z"
        add_repo(conn, r, stars=100 * r, created=created, description=f"repo {r}")
        add_verdict(conn, r, True, substance=1 + r % 4, relevant=0.5 + r / 20)
        ev[f"new{r}"] = add_event(conn, r, "new_repo")
    for r in (20, 21):
        add_repo(conn, r, stars=1000 * (r - 19))
        add_verdict(conn, r, True)
        ev[f"rel{r}"] = add_event(conn, r, "release", {"tag": f"v{r}", "name": "n"})
    add_repo(conn, 30, stars=500, taken_at=t_old)
    add_snapshot(conn, 30, 650, NOW - timedelta(hours=1))
    add_verdict(conn, 30, True)
    add_repo(conn, 31, stars=50)
    add_verdict(conn, 31, True)
    ev["stack31"] = add_event(conn, 31, "stack_changed", {"path": "p.toml", "added": ["a"], "removed": ["b"]})
    add_repo(conn, 40)
    add_verdict(conn, 40, False)
    ev["rej40"] = add_event(conn, 40, "new_repo")
    ev["rejrel40"] = add_event(conn, 40, "release", {"tag": "v9"})
    add_repo(conn, 41)
    ev["untriaged41"] = add_event(conn, 41, "new_repo")
    return ev


# Captured from the code before the insights selection change (flags off must keep producing exactly this).
FLAGS_OFF_SNAPSHOT = (
    "<b>repka</b> · morning digest · 2026-11-07\n"
    "\n"
    "<b>MCP</b>\n"
    '🆕 <a href="https://github.com/o/r7">o/r7</a> ★700 — repo 7 <i>tool · lvl 4</i>\n'
    '🆕 <a href="https://github.com/o/r3">o/r3</a> ★300 — repo 3 <i>tool · lvl 4</i>\n'
    '🆕 <a href="https://github.com/o/r6">o/r6</a> ★600 — repo 6 <i>tool · lvl 3</i>\n'
    '🆕 <a href="https://github.com/o/r2">o/r2</a> ★200 — repo 2 <i>tool · lvl 3</i>\n'
    '🔎 <a href="https://github.com/o/r9">o/r9</a> ★900 — repo 9 <i>tool · lvl 2 · since 2019</i>\n'
    '🆕 <a href="https://github.com/o/r5">o/r5</a> ★500 — repo 5 <i>tool · lvl 2</i>\n'
    '🆕 <a href="https://github.com/o/r1">o/r1</a> ★100 — repo 1 <i>tool · lvl 2</i>\n'
    '🆕 <a href="https://github.com/o/r8">o/r8</a> ★800 — repo 8 <i>tool · lvl 1</i>\n'
    '🆕 <a href="https://github.com/o/r4">o/r4</a> ★400 — repo 4 <i>tool · lvl 1</i>\n'
    '🚀 <a href="https://github.com/o/r21">o/r21</a> <code>v21</code>\n'
    "…and 3 more"
)


def test_flags_off_is_identical_to_before(conn):
    from repka.digest.render import render_digest

    ev = _seed_full_digest(conn)
    d = build(conn, DigestConfig())
    assert "\n".join(render_digest(d)) == FLAGS_OFF_SNAPSHOT
    assert d.shown_event_ids == [ev[k] for k in ("new7", "new3", "new6", "new2", "new9", "new5", "new1", "new8", "new4", "rel21")]
    # rejected repo events, the overflowed release and the overflowed stack change are consumed at once
    assert d.consumed_event_ids == [ev["rej40"], ev["rejrel40"], ev["rel20"], ev["stack31"]]
    assert ev["untriaged41"] not in d.shown_event_ids + d.consumed_event_ids
    assert d.sections[0].more == 3


def test_releases_message_on_leaves_release_events_alone(conn):
    add_repo(conn, 1)
    add_repo(conn, 2)
    add_verdict(conn, 1, True)
    add_verdict(conn, 2, False)
    r1 = add_event(conn, 1, "release", {"tag": "v1"})
    r2 = add_event(conn, 2, "release", {"tag": "v2"})
    n1 = add_event(conn, 1, "new_repo")
    d = build(conn, DigestConfig(releases_message=True))
    assert [i.line_kind for i in items(d)] == ["new"]
    assert [s.releases for s in d.sections] == [[]]
    assert d.shown_event_ids == [n1]
    assert r1 not in d.shown_event_ids + d.consumed_event_ids
    assert r2 not in d.shown_event_ids + d.consumed_event_ids  # rejected repo too: the releases message owns them
    # untouched when the flag is off
    off = build(conn, DigestConfig())
    assert r1 in off.shown_event_ids and r2 in off.consumed_event_ids


def _three_new(conn):
    ids = []
    for r in (1, 2, 3):
        add_repo(conn, r, stars=100 * r)
        add_verdict(conn, r, True, substance=5 - r)  # 1 first, then 2, then 3
        ids.append(add_event(conn, r, "new_repo"))
    return ids


def test_insights_overflow_stays_pending(conn):
    e1, e2, e3 = _three_new(conn)
    d = build(conn, DigestConfig(max_items_per_domain=2, insights=True))
    [section] = d.sections
    assert [i.repo_id for i in section.new_repos] == [1, 2]
    assert d.shown_event_ids == [e1, e2]
    assert section.more == 1 and section.more_by_kind == {"new": 1}
    assert e3 not in d.consumed_event_ids and e3 not in d.shown_event_ids
    assert d.insights is True
    off = build(conn, DigestConfig(max_items_per_domain=2))
    assert off.consumed_event_ids == [e3]
    assert off.insights is False
    assert off.sections[0].more_by_kind == {"new": 1}


def test_insights_overflow_stack_consumed_release_stays_pending(conn):
    for r in (1, 2, 3):
        add_repo(conn, r, stars=100 * r)
        add_verdict(conn, r, True)
    n = add_event(conn, 1, "new_repo")
    s = add_event(conn, 2, "stack_changed", {"path": "p", "added": ["a"], "removed": []})
    rel = add_event(conn, 3, "release", {"tag": "v1"})
    d = build(conn, DigestConfig(max_items_per_domain=1, insights=True))
    assert d.shown_event_ids == [n]
    assert d.consumed_event_ids == [s]  # only the stack overflow is consumed; the release waits for a later digest
    assert rel not in d.shown_event_ids + d.consumed_event_ids
    assert d.sections[0].more_by_kind == {"stack": 1, "release": 1}
    # flag off: the overflowed release is consumed as before
    off = build(conn, DigestConfig(max_items_per_domain=1))
    assert rel in off.consumed_event_ids


def test_insights_release_overflow_behind_new_group_stays_pending(conn):
    ids = _three_new(conn)
    add_repo(conn, 9, stars=90000)
    add_verdict(conn, 9, True)
    rel = add_event(conn, 9, "release", {"tag": "v9"})
    d = build(conn, DigestConfig(max_items_per_domain=2, insights=True))
    [section] = d.sections
    assert [i.repo_id for i in section.new_repos] == [1, 2] and section.releases == []
    assert section.more_by_kind == {"new": 1, "release": 1}
    assert rel not in d.consumed_event_ids and ids[2] not in d.consumed_event_ids
    queue = overflow(conn, DigestConfig(max_items_per_domain=2, insights=True))
    assert sorted((i.line_kind, i.repo_id) for i in items(queue)) == [("new", 1), ("new", 2), ("new", 3), ("release", 9)]


def test_insights_expires_old_pending_releases(conn):
    add_repo(conn, 1)
    add_repo(conn, 2)
    add_verdict(conn, 1, True)
    add_verdict(conn, 2, True)
    old = add_event(conn, 1, "release", {"tag": "v1"}, at=NOW - timedelta(days=8))
    fresh = add_event(conn, 2, "release", {"tag": "v2"}, at=NOW - timedelta(days=6))
    d = build(conn, DigestConfig(insights=True, pending_max_age=timedelta(days=7)))
    assert [i.repo_id for i in items(d)] == [2]
    assert d.shown_event_ids == [fresh] and d.consumed_event_ids == [old]
    off = build(conn, DigestConfig(pending_max_age=timedelta(days=7)))  # flag off: no expiry
    assert sorted(off.shown_event_ids) == sorted([old, fresh])


def test_insights_expires_old_pending_new_repos(conn):
    add_repo(conn, 1)
    add_repo(conn, 2)
    add_verdict(conn, 1, True)
    add_verdict(conn, 2, True)
    old = add_event(conn, 1, "new_repo", at=NOW - timedelta(days=8))
    fresh = add_event(conn, 2, "new_repo", at=NOW - timedelta(days=6))
    d = build(conn, DigestConfig(insights=True, pending_max_age=timedelta(days=7)))
    assert [i.repo_id for i in items(d)] == [2]
    assert d.shown_event_ids == [fresh]
    assert d.consumed_event_ids == [old]
    # flag off: no expiry, the old event is shown as usual
    off = build(conn, DigestConfig(pending_max_age=timedelta(days=7)))
    assert sorted(off.shown_event_ids) == sorted([old, fresh])


def test_insights_order_puts_releases_last(conn):
    t_old = NOW - timedelta(days=2)
    add_repo(conn, 1)
    add_repo(conn, 2)
    add_repo(conn, 3, stars=500, taken_at=t_old)
    add_snapshot(conn, 3, 650, NOW - timedelta(hours=1))
    add_repo(conn, 4)
    for r in (1, 2, 3, 4):
        add_verdict(conn, r, True)
    add_event(conn, 1, "new_repo")
    rel = add_event(conn, 2, "release", {"tag": "v1"})
    st = add_event(conn, 4, "stack_changed", {"path": "p", "added": ["a"], "removed": []})
    on = build(conn, DigestConfig(max_items_per_domain=3, insights=True))
    assert [i.line_kind for i in items(on)] == ["new", "mover", "stack"]
    assert on.sections[0].more_by_kind == {"release": 1}
    assert rel not in on.consumed_event_ids + on.shown_event_ids and st in on.shown_event_ids  # release stays pending
    off = build(conn, DigestConfig(max_items_per_domain=3))
    assert [i.line_kind for i in items(off)] == ["new", "release", "mover"]
    assert off.sections[0].more_by_kind == {"stack": 1}


def overflow(conn, cfg):
    return pending_overflow(conn, [DOMAIN], NOW, cfg, "08:30", "2026-11-07")


@pytest.mark.parametrize("insights", [False, True])
def test_pending_overflow_lists_the_whole_pending_queue(conn, insights):
    _three_new(conn)
    cfg = DigestConfig(max_items_per_domain=2, insights=insights)
    d = overflow(conn, cfg)
    [section] = d.sections
    assert [i.repo_id for i in section.new_repos] == [1, 2, 3]  # the unlimited build, not a subtraction
    assert section.more == 0 and section.more_by_kind == {}
    assert d.item_count == 3 and d.insights is insights
    assert d.shown_event_ids == [] and d.consumed_event_ids == []


def test_pending_overflow_empty_when_the_queue_is_empty(conn):
    assert overflow(conn, DigestConfig(max_items_per_domain=3)).sections == []
    assert overflow(conn, DigestConfig(max_items_per_domain=3)).item_count == 0


def test_pending_overflow_lists_new_found_release_not_stack_or_mover(conn):
    t_old = NOW - timedelta(days=2)
    add_repo(conn, 1)  # new
    add_repo(conn, 2, created="2019-01-01T00:00:00Z")  # found
    add_repo(conn, 3)  # release
    add_repo(conn, 4)  # stack
    add_repo(conn, 5, stars=500, taken_at=t_old)  # mover
    add_snapshot(conn, 5, 650, NOW - timedelta(hours=1))
    for r in (1, 2, 3, 4, 5):
        add_verdict(conn, r, True)
    add_event(conn, 1, "new_repo")
    add_event(conn, 2, "new_repo")
    add_event(conn, 3, "release", {"tag": "v1"})
    add_event(conn, 4, "stack_changed", {"path": "p", "added": ["a"], "removed": []})
    cfg = DigestConfig(max_items_per_domain=10, insights=True, star_spike_min_delta=50)
    assert "mover" in [i.line_kind for i in items(build(conn, cfg))]  # the mover exists in a normal digest
    d = overflow(conn, cfg)
    assert sorted(i.line_kind for i in items(d)) == ["found", "new", "release"]
    assert d.sections[0].movers == [] and d.sections[0].stack_changes == []


def test_pending_overflow_items_carry_facets_with_insights_on(conn):
    add_repo(conn, 1)
    add_verdict(conn, 1, True, extra_answers={"agentic": {"type": "noul", "p": 0.9, "pred": True, "conf": 0.9}})
    add_event(conn, 1, "new_repo")
    [item] = items(overflow(conn, DigestConfig(max_items_per_domain=1, insights=True)))
    assert item.text_fields["facets"] == [("agentic", 0.9)]
    [off] = items(overflow(conn, DigestConfig(max_items_per_domain=1)))
    assert "facets" not in off.text_fields


def test_overflowed_found_item_stays_pending_and_is_listed(conn):
    add_repo(conn, 1)
    add_repo(conn, 2, created="2019-01-01T00:00:00Z")  # old repo found later: "found"
    add_verdict(conn, 1, True, substance=5)
    add_verdict(conn, 2, True, substance=1)
    add_event(conn, 1, "new_repo")
    found = add_event(conn, 2, "new_repo")
    cfg = DigestConfig(max_items_per_domain=1, insights=True)
    d = build(conn, cfg)
    assert d.sections[0].more_by_kind == {"found": 1}
    assert found not in d.consumed_event_ids + d.shown_event_ids
    assert sorted(i.line_kind for i in items(overflow(conn, cfg))) == ["found", "new"]


def test_pending_overflow_writes_nothing(conn):
    _three_new(conn)
    conn.commit()
    before = conn.total_changes
    overflow(conn, DigestConfig(max_items_per_domain=2, insights=True))
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM events WHERE digest_id IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
