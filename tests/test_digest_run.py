import dataclasses
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from helpers_digest import add_event, add_repo, add_verdict, make_db

from repka.cli import main
from repka.config import AppConfig, DigestConfig
from repka.digest import cli as digest_cli
from repka.digest.run import run_digest
from repka.digest.slots import recorded_slots
from repka.domain.schema import Domain, SearchSpec
from repka.notify.telegram_user import SendResult
from repka.store import db as store_db
from repka.store.db import connect

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


def test_recording_a_slot_that_already_exists_does_not_raise(conn):  # a concurrent run recorded it first
    seed_accepted(conn)
    conn.execute("INSERT INTO digests (slot, local_date, status, item_count) VALUES ('19:30', ?, 'sent', 1)", (DUE[1],))
    conn.commit()
    out = run(conn, FakeSender(), slot="19:30")  # the recorded slot is not due again
    assert out.status == "not_due"
    from repka.digest.run import _record_sent
    from repka.digest.build import build_digest

    digest = build_digest(conn, [DOMAIN], NOW, CFG.digest, *DUE)
    _record_sent(conn, digest, NOW)  # IntegrityError before the fix
    assert conn.execute("SELECT COUNT(*) FROM digests WHERE slot = '19:30'").fetchone()[0] == 1


def _home_with_three_repos(home, limit=2, insights=False, events_at=None):
    conn = make_db(home / ".state" / "repka.db")
    for r in (1, 2, 3):
        add_repo(conn, r, stars=100 * r)
        add_verdict(conn, r, True, substance=5 - r)  # 1 first, 3 last
        if events_at is None:
            add_event(conn, r, "new_repo")
        else:
            add_event(conn, r, "new_repo", at=events_at)
    conn.commit()
    conn.close()
    _write_domain(home)
    (home / ".config.yaml").write_text(
        f"digest:\n  max_items_per_domain: {limit}\n  insights: {str(insights).lower()}\n", encoding="utf-8"
    )


def _write_domain(home):
    (home / "domains").mkdir()
    (home / "domains" / "d1.yaml").write_text(
        "id: d1\nname: MCP\nenabled: true\nprofile: p\nsearch:\n  keywords: [[mcp]]\n", encoding="utf-8"
    )


def test_cli_more_prints_the_pending_queue_and_exits_0(home, monkeypatch, capsys):
    _home_with_three_repos(home)
    monkeypatch.setattr(digest_cli, "make_sender", lambda cfg: Boom())
    assert main(["digest", "--more"]) == 0
    out = capsys.readouterr().out
    assert "o/r3" in out and "o/r1" in out and "o/r2" in out  # the whole queue, not only the overflow
    check = make_db(home / ".state" / "repka.db")
    assert check.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    assert check.execute("SELECT COUNT(*) FROM events WHERE digest_id IS NOT NULL").fetchone()[0] == 0
    check.close()


def test_cli_more_after_a_send_lists_what_was_held_back(home, monkeypatch, capsys):
    _home_with_three_repos(home, limit=2, insights=True, events_at=datetime.now(UTC))
    sender = FakeSender()
    monkeypatch.setattr(digest_cli, "make_sender", lambda cfg: sender)
    (home / ".config.yaml").write_text(
        "digest:\n  max_items_per_domain: 2\n  insights: true\nnotify:\n  enabled: true\n", encoding="utf-8"
    )
    assert main(["digest", "--send", "--slot", "08:30"]) == 0
    assert "o/r3" not in "".join(sender.calls[0]) and "o/r1" in "".join(sender.calls[0])
    capsys.readouterr()
    assert main(["digest", "--more"]) == 0
    out = capsys.readouterr().out
    assert "o/r3" in out and "o/r1" not in out and "o/r2" not in out
    assert out.splitlines()[0].startswith("[more]") and ": 1 pending item(s)" in out.splitlines()[0]


def test_pending_overflow_after_a_send_lists_every_held_back_item(conn):
    from repka.digest.build import pending_overflow

    for r in range(1, 6):
        add_repo(conn, r, stars=100 * r)
        add_verdict(conn, r, True, substance=6 - r)
        add_event(conn, r, "new_repo")
    cfg = dataclasses.replace(CFG, digest=DigestConfig(insights=True, max_items_per_domain=2))
    sender = FakeSender()
    out = run_digest(conn, [DOMAIN], cfg, sender, NOW, True, tz=TZ)
    assert out.status == "sent" and out.item_count == 2  # "...and 3 more"
    assert "…and 3 more" in sender.calls[0][-1]
    more = pending_overflow(conn, [DOMAIN], NOW, cfg.digest, *DUE)
    assert more.item_count == 3 and [i.repo_id for i in more.sections[0].new_repos] == [3, 4, 5]


def _v3_home(home, seed=False):
    """A repka home whose database stops at migration 003 (before `digests.kind`)."""
    conn = connect(home / ".state" / "repka.db")
    for number, sql in store_db._migrations():
        if number <= 3:
            conn.executescript(sql)
    if seed:
        add_repo(conn, 1)
        add_verdict(conn, 1, True)
        add_event(conn, 1, "new_repo")
    conn.commit()
    conn.close()
    _write_domain(home)


@pytest.mark.parametrize("flag", [[], ["--dry-run"]])
def test_cli_digest_on_an_unmigrated_database_is_a_config_error(home, capsys, flag):
    _v3_home(home)
    assert main(["digest", *flag]) == 2
    err = capsys.readouterr().err
    assert "older than this repka" in err and "repka db migrate" in err
    assert "Traceback" not in err
    check = sqlite3.connect(home / ".state" / "repka.db")
    assert check.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3  # nothing was migrated
    check.close()


def test_cli_more_on_a_database_without_tables_is_a_config_error(home, capsys):
    (home / ".state").mkdir()
    sqlite3.connect(home / ".state" / "repka.db").close()  # a file with no schema at all
    _write_domain(home)
    assert main(["digest", "--more"]) == 2
    err = capsys.readouterr().err
    assert "older than this repka" in err and "Traceback" not in err


def test_cli_send_migrates_an_old_database_first(home, monkeypatch, capsys):
    _v3_home(home, seed=True)
    (home / ".config.yaml").write_text("notify:\n  enabled: true\n", encoding="utf-8")
    sender = FakeSender()
    monkeypatch.setattr(digest_cli, "make_sender", lambda cfg: sender)
    assert main(["digest", "--send", "--slot", "08:30"]) == 0
    assert len(sender.calls) == 1 and "o/r1" in "".join(sender.calls[0])
    check = make_db(home / ".state" / "repka.db")
    assert check.execute("SELECT kind FROM digests").fetchall()[0][0] == "main"
    check.close()


def test_cli_more_nothing_waiting(home, capsys):
    _home_with_three_repos(home, limit=10)
    conn = make_db(home / ".state" / "repka.db")
    conn.execute("DELETE FROM events")
    conn.commit()
    conn.close()
    assert main(["digest", "--more"]) == 0
    assert capsys.readouterr().out.strip() == "nothing waiting in the queue"


def test_cli_more_without_database_is_a_config_error(home, capsys):
    assert main(["digest", "--more"]) == 2
    assert "no database" in capsys.readouterr().err


def test_cli_more_conflicts_with_send(home, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["digest", "--more", "--send"])
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err
