"""`repka releases`: recent release events of accepted repos, read-only, one section per domain."""

from datetime import UTC, datetime, timedelta

from helpers_digest import add_event, add_repo, add_verdict, make_db

from repka.cli import build_parser, main


def _ago(**kw) -> datetime:
    return datetime.now(UTC) - timedelta(**kw)


def _home(home, domains=("d1",)):
    conn = make_db(home / ".state" / "repka.db")
    (home / "domains").mkdir(exist_ok=True)
    for d in domains:
        (home / "domains" / f"{d}.yaml").write_text(
            f"id: {d}\nname: Dom {d}\nenabled: true\nprofile: p\nsearch:\n  keywords: [[mcp]]\n", encoding="utf-8"
        )
    return conn


def _repo(conn, repo_id, stars=100, full_name=None, accepted=True, domain="d1"):
    add_repo(conn, repo_id, full_name=full_name, stars=stars)
    add_verdict(conn, repo_id, accepted, domain=domain)


def _release(conn, repo_id, tag, at, domain="d1"):
    return add_event(
        conn,
        repo_id,
        "release",
        {"tag": tag, "name": tag, "published_at": "2026-11-09T10:00:00Z"},
        domain=domain,
        at=at,
    )


def _mark_sent(conn, event_id):
    cur = conn.execute(
        "INSERT INTO digests (slot, local_date, status, item_count, sent_at) VALUES ('19:30', '2026-11-09', 'sent', 1, ?)",
        ("2026-11-09T15:30:00Z",),
    )
    conn.execute("UPDATE events SET digest_id = ? WHERE id = ?", (cur.lastrowid, event_id))


def test_every_command_registered():
    subcommands = build_parser()._subparsers._group_actions[0].choices
    assert "releases" in subcommands


def test_lists_sent_and_pending_events(home, capsys):
    conn = _home(home)
    _repo(conn, 1, stars=10, full_name="o/pending")
    _repo(conn, 2, stars=20, full_name="o/sent")
    _release(conn, 1, "v1.0", _ago(hours=2))
    sent = _release(conn, 2, "v2.0", _ago(hours=3))
    _mark_sent(conn, sent)
    conn.commit()
    conn.close()
    assert main(["releases"]) == 0
    out = capsys.readouterr().out
    assert "o/pending" in out and "v1.0" in out
    assert "o/sent" in out and "v2.0" in out


def test_since_window_excludes_old(home, capsys):
    conn = _home(home)
    _repo(conn, 1, full_name="o/old")
    _release(conn, 1, "v1.0", _ago(days=3))
    conn.commit()
    conn.close()
    assert main(["releases"]) == 0
    out = capsys.readouterr().out
    assert "o/old" not in out
    assert "no releases in the last 24h" in out
    assert main(["releases", "--since", "7d"]) == 0
    assert "o/old" in capsys.readouterr().out


def test_rejected_repo_hidden(home, capsys):
    conn = _home(home)
    _repo(conn, 1, full_name="o/accepted")
    _repo(conn, 2, full_name="o/rejected", accepted=False)
    _release(conn, 1, "v1.0", _ago(hours=1))
    _release(conn, 2, "v9.9", _ago(hours=1))
    conn.commit()
    conn.close()
    assert main(["releases"]) == 0
    out = capsys.readouterr().out
    assert "o/accepted" in out
    assert "o/rejected" not in out and "v9.9" not in out


def test_latest_event_per_repo_sorted_by_stars(home, capsys):
    conn = _home(home)
    _repo(conn, 1, stars=10, full_name="o/small")
    _repo(conn, 2, stars=500, full_name="o/big")
    _release(conn, 1, "v1.0", _ago(hours=5))
    _release(conn, 1, "v1.1", _ago(hours=1))
    _release(conn, 2, "v2.0", _ago(hours=2))
    conn.commit()
    conn.close()
    assert main(["releases"]) == 0
    out = capsys.readouterr().out
    assert "v1.0" not in out and "v1.1" in out
    assert out.index("o/big") < out.index("o/small")
    assert "★500" in out and "★10" in out


def test_domain_filter(home, capsys):
    conn = _home(home, domains=("d1", "d2"))
    _repo(conn, 1, full_name="o/in-d1", domain="d1")
    _repo(conn, 2, full_name="o/in-d2", domain="d2")
    _release(conn, 1, "v1.0", _ago(hours=1), domain="d1")
    _release(conn, 2, "v2.0", _ago(hours=1), domain="d2")
    conn.commit()
    conn.close()
    assert main(["releases", "--domain", "d2"]) == 0
    out = capsys.readouterr().out
    assert "o/in-d2" in out and "o/in-d1" not in out


def test_empty_window_message(home, capsys):
    conn = _home(home)
    conn.commit()
    conn.close()
    assert main(["releases"]) == 0
    assert "no releases in the last 24h" in capsys.readouterr().out


def test_untrusted_name_is_printable(home, capsys):
    conn = _home(home)
    _repo(conn, 1, full_name="o/evil\x1b[2J")
    _release(conn, 1, "v1.0\x1b[31m", _ago(hours=1))
    conn.commit()
    conn.close()
    assert main(["releases"]) == 0
    out = capsys.readouterr().out
    assert "o/evil" in out and "v1.0" in out
    assert "\x1b" not in out


def test_missing_db_exits_2(home, capsys):
    assert main(["releases"]) == 2
    assert "no database" in capsys.readouterr().err


def test_bad_since_exits_2(home, capsys):
    conn = _home(home)
    conn.close()
    assert main(["releases", "--since", "soon"]) == 2
    assert "error:" in capsys.readouterr().err
