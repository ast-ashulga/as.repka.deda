from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from helpers_digest import add_event, add_repo, add_verdict, make_db

from repka.config import AppConfig, DigestConfig
from repka.digest.releases import build_releases, render_releases, run_releases
from repka.digest.slots import recorded_slots
from repka.domain.schema import Domain, SearchSpec
from repka.notify.telegram_user import SendResult

TZ = ZoneInfo("Asia/Tbilisi")
NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)  # 10:00 in Tbilisi: the 09:00 releases slot is due
DOMAIN = Domain(id="d1", name="MCP", profile="p", search=SearchSpec((), (), (), ()))
CFG = AppConfig(digest=DigestConfig(releases_message=True))
DUE = ("09:00", "2026-11-07")
SLOT_ROW = "releases:09:00"


class FakeSender:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def send(self, parts):
        self.calls.append(parts)
        return self.results.pop(0) if self.results else SendResult(ok=True, sent=len(parts))


class Boom:
    def send(self, parts):
        raise AssertionError("must not send")


@pytest.fixture
def conn(tmp_path):
    c = make_db(tmp_path / "t.db")
    yield c
    c.close()


def seed(conn, repo_id, tag, stars=100, accepted=True, full_name=None, event=True):
    add_repo(conn, repo_id, full_name=full_name, stars=stars)
    if accepted is not None:
        add_verdict(conn, repo_id, accepted)
    return add_event(conn, repo_id, "release", {"tag": tag, "name": tag}) if event else None


def build(conn, cfg=CFG.digest):
    return build_releases(conn, [DOMAIN], NOW, cfg, *DUE)


def run(conn, sender, send=True, now=NOW, cfg=CFG, **kw):
    return run_releases(conn, [DOMAIN], cfg, sender, now, send, tz=TZ, **kw)


def test_latest_release_per_repo_sorted_by_stars(conn):
    older = seed(conn, 1, "v1.0", stars=50)
    newer = add_event(conn, 1, "release", {"tag": "v1.1"})
    seed(conn, 2, "v9.0", stars=500)
    d = build(conn)
    items = d.sections[0].items
    assert [(i.repo_id, i.text_fields["tag"]) for i in items] == [(2, "v9.0"), (1, "v1.1")]
    assert newer in d.shown_event_ids and older in d.consumed_event_ids and older not in d.shown_event_ids
    assert d.item_count == 2


def test_rejected_repo_release_consumed_not_shown(conn):  # Review Focus 2
    shown = seed(conn, 1, "v1")
    rejected = seed(conn, 2, "v2", accepted=False)
    untriaged = seed(conn, 3, "v3", accepted=None)
    d = build(conn)
    assert d.shown_event_ids == [shown]
    assert d.consumed_event_ids == [rejected]
    assert untriaged not in d.shown_event_ids + d.consumed_event_ids
    assert [i.repo_id for s in d.sections for i in s.items] == [1]


def test_only_release_events_are_taken(conn):
    seed(conn, 1, "v1")
    other = add_event(conn, 1, "new_repo")
    d = build(conn)
    assert other not in d.shown_event_ids + d.consumed_event_ids


def test_limit_and_more_counter(conn):
    ids = [seed(conn, n, f"v{n}", stars=1000 - n) for n in range(1, 13)]
    d = build(conn, replace(CFG.digest, max_items_per_domain=10))
    sec = d.sections[0]
    assert len(sec.items) == 10 and sec.more == 2
    assert d.shown_event_ids == ids[:10] and d.consumed_event_ids == ids[10:]
    lines = render_releases(d)
    assert sum(1 for line in lines if line.startswith("🚀")) == 10
    assert "…and 2 more" in lines
    assert "<b>MCP</b> · 12 releases" in lines


def test_render_releases_exact_lines(conn):
    seed(conn, 1, "v3.74.1", stars=81_000, full_name="o/r1")
    seed(conn, 2, "v1.0", stars=500, full_name="o/r2")
    lines = render_releases(build(conn))
    assert lines == [
        "<b>repka</b> · releases · 2026-11-07",
        "",
        "<b>MCP</b> · 2 releases",
        '🚀 <a href="https://github.com/o/r1">o/r1</a> ★81k · <code>v3.74.1</code>',
        '🚀 <a href="https://github.com/o/r2">o/r2</a> ★500 · <code>v1.0</code>',
        "",
        "Full list: repka releases --since 24h",
    ]


def test_render_escapes_untrusted_text(conn):
    seed(conn, 1, "<b>v1</b>\x07", full_name="o/<script>")
    text = "\n".join(render_releases(build(conn)))
    assert "<script>" not in text and "<b>v1</b>" not in text and "\x07" not in text
    assert "&lt;script&gt;" in text


def test_render_empty_is_empty_list(conn):
    assert render_releases(build(conn)) == []


def test_failed_send_keeps_events_pending_then_sent_once(conn):  # Review Focus 3
    seed(conn, 1, "v1")
    seed(conn, 2, "v2")
    out = run(conn, FakeSender(SendResult(ok=False, sent=0, error="x")))
    assert out.status == "failed" and out.error == "x"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE digest_id IS NOT NULL").fetchone()[0] == 0
    # nothing recorded for the due slot (the stale slot of yesterday is legitimately recorded as skipped)
    assert conn.execute("SELECT COUNT(*) FROM digests WHERE slot = ? AND local_date = ?", (SLOT_ROW, DUE[1])).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM digest_items").fetchone()[0] == 0

    sender = FakeSender()
    out = run(conn, sender)
    assert out.status == "sent" and out.item_count == 2 and len(sender.calls) == 1
    row = conn.execute("SELECT id, slot, kind, status, item_count, sent_at FROM digests WHERE slot = ? AND local_date = ?", (SLOT_ROW, DUE[1])).fetchone()
    assert (row["slot"], row["status"], row["item_count"], row["sent_at"]) == (SLOT_ROW, "sent", 2, "2026-11-07T06:00:00Z")
    assert {r["digest_id"] for r in conn.execute("SELECT digest_id FROM events")} == {row["id"]}
    assert sorted(tuple(r) for r in conn.execute("SELECT repo_id, kind FROM digest_items")) == [(1, "release"), (2, "release")]

    again = Boom()
    assert run(conn, again).status == "not_due"


def test_releases_slot_equal_to_main_slot_does_not_collide(conn):  # Review Focus 5
    cfg = AppConfig(digest=DigestConfig(releases_message=True, releases_slot="19:30"))
    now = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)  # 20:00 local
    conn.execute(
        "INSERT INTO digests (slot, local_date, status, sent_at) VALUES ('19:30', '2026-11-07', 'sent', '2026-11-07T15:31:00Z')"
    )
    seed(conn, 1, "v1")
    sender = FakeSender()
    out = run(conn, sender, now=now, cfg=cfg)
    assert out.status == "sent" and len(sender.calls) == 1
    rows = [tuple(r) for r in conn.execute("SELECT slot, kind, status FROM digests WHERE local_date = '2026-11-07' ORDER BY id")]
    assert rows == [("19:30", "main", "sent"), ("releases:19:30", "releases", "sent")]
    assert recorded_slots(conn, "2026-11-07") == {("19:30", "2026-11-07")}
    assert recorded_slots(conn, "2026-11-07", kind="releases") == {("19:30", "2026-11-07")}


def test_empty_releases_not_sent_and_recorded_empty(conn):
    sender = FakeSender()
    out = run(conn, sender)
    assert out.status == "empty" and sender.calls == []
    row = conn.execute("SELECT slot, kind, status, item_count FROM digests WHERE kind = 'releases' AND local_date = ?", (DUE[1],)).fetchone()
    assert tuple(row) == (SLOT_ROW, "releases", "empty", 0)


def test_rejected_only_is_empty_and_not_sent(conn):
    seed(conn, 1, "v1", accepted=False)
    sender = FakeSender()
    assert run(conn, sender).status == "empty" and sender.calls == []


def test_preview_writes_nothing(conn):
    seed(conn, 1, "v1")
    conn.commit()
    before = conn.total_changes
    out = run(conn, Boom(), send=False)
    assert out.status == "preview" and out.item_count == 1 and out.parts
    assert conn.total_changes == before


def test_preview_of_nothing_is_empty(conn):
    out = run(conn, Boom(), send=False)
    assert out.status == "empty" and out.parts == []


def test_missed_slot_older_than_max_lateness_is_skipped_events_stay_pending(conn):
    ev = seed(conn, 1, "v1")
    late = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)  # 16:00 local: 09:00 is 7h old (> 6h)
    out = run(conn, Boom(), now=late)
    assert out.status == "not_due"
    rows = {(r["slot"], r["local_date"]): r["status"] for r in conn.execute("SELECT slot, local_date, status FROM digests")}
    assert rows[(SLOT_ROW, "2026-11-07")] == "skipped"
    assert conn.execute("SELECT digest_id FROM events WHERE id = ?", (ev,)).fetchone()["digest_id"] is None
    # the next day's slot picks the event up
    nxt = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
    sender = FakeSender()
    assert run(conn, sender, now=nxt).status == "sent"
    assert "v1" in sender.calls[0][0]


def test_send_requires_sender(conn):
    with pytest.raises(ValueError):
        run(conn, None)


def test_now_must_be_utc(conn):
    with pytest.raises(ValueError):
        run(conn, FakeSender(), now=datetime(2026, 11, 7, 10, 0, tzinfo=TZ))


def test_main_digest_rows_do_not_make_releases_slot_recorded(conn):
    conn.execute("INSERT INTO digests (slot, local_date, status) VALUES ('09:00', '2026-11-07', 'sent')")
    seed(conn, 1, "v1")
    assert run(conn, FakeSender()).status == "sent"
