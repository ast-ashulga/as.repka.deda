from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from repka.digest.slots import decide_slot, recorded_slots, slot_key

TZ = ZoneInfo("Asia/Tbilisi")
SLOTS = ["08:30", "19:30"]
LATE = timedelta(hours=6)
TODAY, YESTERDAY = "2026-11-07", "2026-11-06"


def at(hour, minute=0):
    return datetime(2026, 11, 7, hour, minute, tzinfo=TZ)


def test_slot_not_due_before_first_slot():
    d = decide_slot(SLOTS, at(7), {("08:30", YESTERDAY), ("19:30", YESTERDAY)}, LATE)
    assert d.due is None and d.skipped == []


def test_missed_slots_coalesce():  # RF5: now 20:00, nothing recorded -> due 19:30; 08:30 and yesterday's are skipped
    d = decide_slot(SLOTS, at(20), set(), LATE)
    assert d.due == ("19:30", TODAY)
    assert d.skipped == [("08:30", YESTERDAY), ("19:30", YESTERDAY), ("08:30", TODAY)]


def test_stale_slot_skipped():  # RF5: now 03:00, yesterday 19:30 is 7.5h late (> 6h)
    d = decide_slot(SLOTS, at(3), set(), LATE)
    assert d.due is None
    assert ("19:30", YESTERDAY) in d.skipped


def test_yesterday_evening_after_midnight_within_lateness():
    d = decide_slot(SLOTS, at(0, 30), {("08:30", YESTERDAY)}, LATE)
    assert d.due == ("19:30", YESTERDAY) and d.skipped == []


def test_recorded_slots_reads_all_statuses(tmp_path):
    from helpers_digest import make_db

    conn = make_db(tmp_path / "t.db")
    for slot, day, status in (("08:30", TODAY, "sent"), ("19:30", YESTERDAY, "empty"), ("08:30", "2026-10-01", "sent")):
        conn.execute("INSERT INTO digests (slot, local_date, status) VALUES (?, ?, ?)", (slot, day, status))
    assert recorded_slots(conn, YESTERDAY) == {("08:30", TODAY), ("19:30", YESTERDAY)}
    conn.close()


def test_main_recorded_slots_ignore_releases_rows(tmp_path):
    from helpers_digest import make_db

    conn = make_db(tmp_path / "t.db")
    conn.execute("INSERT INTO digests (slot, local_date, status) VALUES ('08:30', ?, 'sent')", (TODAY,))
    conn.execute(
        "INSERT INTO digests (slot, local_date, status, kind) VALUES (?, ?, 'sent', 'releases')",
        (slot_key("releases", "09:00"), TODAY),
    )
    assert recorded_slots(conn, YESTERDAY) == {("08:30", TODAY)}
    assert recorded_slots(conn, YESTERDAY, kind="releases") == {("09:00", TODAY)}
    assert slot_key("main", "08:30") == "08:30" and slot_key("releases", "09:00") == "releases:09:00"
    conn.close()
