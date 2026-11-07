from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from helpers_digest import add_event, add_repo, add_verdict, make_db

from repka.cli import main
from repka.config import AppConfig
from repka.digest import cli as digest_cli
from repka.digest.run import run_digest
from repka.digest.slots import recorded_slots
from repka.domain.schema import Domain, SearchSpec
from repka.notify.telegram_user import SendResult

TZ = ZoneInfo("Asia/Tbilisi")
NOW = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)  # 20:00 in Tbilisi: slot 19:30 is due, 08:30 is missed
DOMAIN = Domain(id="d1", name="MCP", profile="p", search=SearchSpec((), (), (), ()))
CFG = AppConfig()
DUE = ("19:30", "2026-11-07")


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


def seed_accepted(conn, repo_id=1):
    add_repo(conn, repo_id)
    add_verdict(conn, repo_id, True)
    return add_event(conn, repo_id, "new_repo")


def run(conn, sender, send=True, now=NOW, **kw):
    return run_digest(conn, [DOMAIN], CFG, sender, now, send, tz=TZ, **kw)


def test_empty_digest_not_sent(conn):  # RF5
    sender = FakeSender()
    out = run(conn, sender)
    assert out.status == "empty" and sender.calls == []
    row = conn.execute("SELECT status, item_count FROM digests WHERE slot = '19:30' AND local_date = ?", (DUE[1],)).fetchone()
    assert (row["status"], row["item_count"]) == ("empty", 0)


def test_preview_changes_no_state(conn):
    seed_accepted(conn)
    conn.commit()
    before = conn.total_changes
    out = run(conn, Boom(), send=False)
    assert out.status == "preview" and out.item_count == 1 and out.parts
    assert conn.total_changes == before


def test_send_marks_events_and_records_items(conn):
    shown = seed_accepted(conn, 1)
    add_repo(conn, 2)
    add_verdict(conn, 2, False)
    consumed = add_event(conn, 2, "new_repo")
    untriaged = (add_repo(conn, 3), add_event(conn, 3, "new_repo"))[1]
    sender = FakeSender()
    out = run(conn, sender)
    assert out.status == "sent" and len(sender.calls) == 1
    digest = conn.execute(
        "SELECT id, status, item_count, sent_at FROM digests WHERE slot = '19:30' AND local_date = ?", (DUE[1],)
    ).fetchone()
    assert (digest["status"], digest["item_count"], digest["sent_at"]) == ("sent", 1, "2026-11-07T16:00:00Z")
    stamp = {r["id"]: r["digest_id"] for r in conn.execute("SELECT id, digest_id FROM events")}
    assert stamp[shown] == stamp[consumed] == digest["id"] and stamp[untriaged] is None
    items = [tuple(r) for r in conn.execute("SELECT repo_id, kind FROM digest_items")]
    assert items == [(1, "new")]


def test_send_failure_leaves_slot_open_for_retry(conn):
    seed_accepted(conn)
    failing = FakeSender(SendResult(ok=False, sent=0, error="flood wait"))
    out = run(conn, failing)
    assert out.status == "failed" and out.error == "flood wait"
    assert DUE not in recorded_slots(conn, "2026-11-06")
    assert conn.execute("SELECT COUNT(*) FROM digest_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE digest_id IS NOT NULL").fetchone()[0] == 0
    retry = run(conn, FakeSender())  # next run, still within max_lateness
    assert retry.status == "sent" and DUE in recorded_slots(conn, "2026-11-06")


def test_partial_delivery_counts_as_sent(conn):
    seed_accepted(conn)
    out = run(conn, FakeSender(SendResult(ok=False, sent=1, error="part 2 failed")))
    assert out.status == "sent" and out.error == "part 2 failed"
    assert DUE in recorded_slots(conn, "2026-11-06")


def test_no_double_digest_same_slot(conn):
    seed_accepted(conn)
    assert run(conn, FakeSender()).status == "sent"
    again = FakeSender()
    assert run(conn, again).status == "not_due" and again.calls == []
    assert run(conn, again, slot="19:30").status == "not_due"  # a forced slot is refused too


def test_send_records_skipped_slots(conn):
    run(conn, FakeSender())
    rows = {(r["slot"], r["local_date"]): r["status"] for r in conn.execute("SELECT * FROM digests")}
    assert rows[("08:30", "2026-11-07")] == "skipped" and rows[DUE] == "empty"


def test_now_must_be_utc(conn):
    with pytest.raises(ValueError):
        run(conn, FakeSender(), now=datetime(2026, 11, 7, 20, 0, tzinfo=TZ))


def test_send_refused_when_notify_disabled(home, capsys):
    assert main(["digest", "--send"]) == 2
    assert "notify.enabled" in capsys.readouterr().err


def test_default_cli_previews_to_stdout_without_sending(home, monkeypatch, capsys):
    conn = make_db(home / ".state" / "repka.db")
    seed_accepted(conn)
    conn.commit()
    conn.close()
    (home / "domains").mkdir()
    (home / "domains" / "d1.yaml").write_text(
        "id: d1\nname: MCP\nenabled: true\nprofile: p\nsearch:\n  keywords: [[mcp]]\n", encoding="utf-8"
    )
    monkeypatch.setattr(digest_cli, "make_sender", lambda cfg: Boom())
    assert main(["digest"]) == 0
    out = capsys.readouterr().out
    assert "[preview]" in out and "o/r1" in out
    check = make_db(home / ".state" / "repka.db")
    assert check.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    check.close()
