import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import repka.collect as collect_mod
import repka.collect_cli as collect_cli
from repka.cli import main
from repka.collect import collect_domains, due_domains
from repka.config import AppConfig, state_dir
from repka.domain.schema import Domain, SearchSpec
from repka.github.client import GitHubError, RateLimited
from repka.github.enrich import EnrichStats
from repka.github.search import QueryStats
from repka.models import RepoHit
from repka.runlock import RunLock
from repka.store import repo
from repka.store.db import connect, migrate
from repka.store.repo import DomainState

NOW = datetime(2026, 11, 8, 8, 0, 0, tzinfo=UTC)
NOW_S = "2026-11-08T08:00:00Z"
CFG = AppConfig()


def domain(domain_id="alpha", enabled=True, scan_every=timedelta(hours=12)) -> Domain:
    return Domain(
        id=domain_id,
        name=domain_id,
        enabled=enabled,
        scan_every=scan_every,
        profile="p",
        search=SearchSpec(keywords=(), topics=("mcp",), languages=(), exclude=()),
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    yield c
    c.close()


def hit(repo_id: int) -> RepoHit:
    return RepoHit(repo_id, f"N{repo_id}", f"o/r{repo_id}", f"https://x/{repo_id}", None, None, (), 1, 0, False,
                   NOW, NOW, False, None)


class Fakes:
    """Replaces collect_search / enrich_repos in repka.collect and records how they were called."""

    def __init__(self, monkeypatch, search=None):
        self.searches: list[dict] = []
        self.enriched: list[dict] = []
        self._search = search
        self.skipped = 0  # repos enrichment reports as skipped
        monkeypatch.setattr(collect_mod, "collect_search", self.collect_search)
        monkeypatch.setattr(collect_mod, "enrich_repos", self.enrich_repos)

    def collect_search(self, client, conn, domain, window, now, max_repos, emit_new, *, per_page, progress=None):
        self.searches.append({"domain": domain.id, "now": now, "emit_new": emit_new, "window": window, "per_page": per_page})
        if self._search:
            return self._search(conn, domain, now)
        return QueryStats(hits=1), set()

    def enrich_repos(self, client, conn, domain_id, repo_ids, key_files, now, cfg, slow_for, baseline=False, progress=None, **kw):
        self.enriched.append(
            {"domain": domain_id, "key_files_by_domain": kw.get("key_files_by_domain"), "ids": list(repo_ids), "slow_for": set(slow_for), "now": now, "baseline": baseline}
        )
        return EnrichStats(repos=len(repo_ids), skipped=self.skipped)


def raise_rate_limited(conn, domain, now):
    raise RateLimited("slow down", None)


def test_due_domains_logic():
    states = {
        "recent": DomainState("recent", "2026-11-08T07:00:00Z", "ok"),
        "old": DomainState("old", "2026-11-07T07:00:00Z", "ok"),
        "resume": DomainState("resume", "2026-11-08T07:30:00Z", "partial"),
    }
    domains = [domain("never"), domain("recent"), domain("old"), domain("resume"), domain("off", enabled=False)]
    assert [d.id for d in due_domains(domains, states, NOW)] == ["never", "old", "resume"]


def test_rate_limit_stops_remaining_domains_and_marks_partial(conn, monkeypatch):
    fakes = Fakes(monkeypatch, search=raise_rate_limited)
    report = collect_domains(None, conn, [domain("a"), domain("b")], CFG, NOW)
    assert report.rate_limited is True
    assert [(r.domain_id, r.status) for r in report.domains] == [("a", "partial"), ("b", "skipped")]
    assert "rate limit" in report.domains[0].error
    assert [s["domain"] for s in fakes.searches] == ["a"]  # b was never searched
    assert repo.get_domain_state(conn, "a").last_scan_status == "partial"


def test_github_error_in_one_domain_does_not_skip_the_others(conn, monkeypatch):
    def search(conn, d, now):
        if d.id == "a":
            raise GitHubError("HTTP 502")
        return QueryStats(hits=1), set()

    Fakes(monkeypatch, search=search)
    report = collect_domains(None, conn, [domain("a"), domain("b")], CFG, NOW)
    assert [(r.domain_id, r.status) for r in report.domains] == [("a", "partial"), ("b", "ok")]
    assert "HTTP 502" in report.domains[0].error and report.rate_limited is False


def test_enrichment_knows_the_key_files_of_domains_that_are_not_due(conn, monkeypatch):
    fakes = Fakes(monkeypatch)
    other = replace(domain("b"), key_files=("go.mod",))
    collect_domains(None, conn, [domain("a")], CFG, NOW, all_domains=[domain("a"), other])
    assert fakes.enriched[0]["key_files_by_domain"]["b"] == ("go.mod",)


def test_last_scan_not_advanced_on_partial(conn, monkeypatch):
    repo.set_domain_scanned(conn, "a", "2026-11-01T00:00:00Z", "ok", "h", 3600)
    Fakes(monkeypatch, search=raise_rate_limited)
    collect_domains(None, conn, [domain("a")], CFG, NOW)
    state = repo.get_domain_state(conn, "a")
    assert (state.last_scan_at, state.last_scan_status) == ("2026-11-01T00:00:00Z", "partial")


def test_enrich_skips_end_the_baseline_but_mark_the_scan_partial(conn, monkeypatch):
    """Documented in the module docstring: repos whose node kept failing are retried next run, they do not keep
    the whole domain in first-scan mode (only a scan cut short does, see the test above)."""
    fakes = Fakes(monkeypatch)
    fakes.skipped = 1
    report = collect_domains(None, conn, [domain()], CFG, NOW)
    assert [r.status for r in report.domains] == ["partial"] and "skipped" in report.domains[0].error
    state = repo.get_domain_state(conn, "alpha")
    assert (state.last_scan_at, state.last_scan_status) == (NOW_S, "partial")
    collect_domains(None, conn, [domain()], CFG, NOW + timedelta(hours=1))  # partial: due again at once
    assert [s["emit_new"] for s in fakes.searches] == [False, True]


def test_interrupted_first_scan_stays_a_silent_baseline(conn, monkeypatch):
    fakes = Fakes(monkeypatch, search=raise_rate_limited)
    collect_domains(None, conn, [domain()], CFG, NOW)  # first scan interrupted
    assert repo.get_domain_state(conn, "alpha").last_scan_at is None
    fakes._search = None
    collect_domains(None, conn, [domain()], CFG, NOW + timedelta(hours=1))  # first scan completes
    assert repo.get_domain_state(conn, "alpha").last_scan_at == "2026-11-08T09:00:00Z"
    collect_domains(None, conn, [domain()], CFG, NOW + timedelta(hours=13))  # now a real baseline exists
    assert [s["emit_new"] for s in fakes.searches] == [False, False, True]
    assert [e["baseline"] for e in fakes.enriched] == [True, False]  # silent exactly during the baseline (the interrupted scan never reached enrich)
    assert fakes.searches[2]["window"].start == datetime(2026, 11, 7).date()  # stored last_scan_at - overlap
    assert fakes.searches[0]["per_page"] == CFG.github.per_page


def test_unfinished_scan_keeps_its_window_across_days(conn, monkeypatch):
    fakes = Fakes(monkeypatch, search=raise_rate_limited)
    collect_domains(None, conn, [domain()], CFG, NOW)  # day 1: interrupted
    fakes._search = None
    collect_domains(None, conn, [domain()], CFG, NOW + timedelta(days=1))  # day 2: resumes on day 1's window
    first, second = (s["window"] for s in fakes.searches)
    assert second == first
    assert repo.saved_window(conn, "alpha") is None  # cleared with the cursors once the scan completed
    # the scan finished on a window that ends yesterday: the next window starts there, so no day is skipped
    assert repo.get_domain_state(conn, "alpha").last_scan_at == "2026-11-08T00:00:00Z"
    collect_domains(None, conn, [domain()], CFG, NOW + timedelta(days=2))
    assert fakes.searches[2]["window"].start == datetime(2026, 11, 7).date()  # 11-08 minus the 1-day overlap
    assert fakes.searches[2]["window"].end == datetime(2026, 11, 10).date()


def test_complete_scan_clears_cursors_partial_keeps_them(conn, monkeypatch):
    def search(conn, domain, now):
        repo.mark_cursor(conn, domain.id, "q", "pushed:a..b", 1, False, now)
        return QueryStats(hits=1), set()

    Fakes(monkeypatch, search=search)
    tiny = AppConfig(collect=type(CFG.collect)(max_repos_per_domain_scan=1))
    collect_domains(None, conn, [domain()], tiny, NOW)  # hits (1) reached the budget -> partial
    assert repo.cursor_done(conn, "alpha", "q", "pushed:a..b")
    assert repo.get_domain_state(conn, "alpha").last_scan_status == "partial"
    collect_domains(None, conn, [domain()], CFG, NOW + timedelta(hours=1))  # budget not reached -> ok
    assert not repo.cursor_done(conn, "alpha", "q", "pushed:a..b")
    assert repo.get_domain_state(conn, "alpha").last_scan_status == "ok"


def test_enrich_targets_cover_earlier_partial_run_and_accepted(conn, monkeypatch):
    last = "2026-11-05T00:00:00Z"
    repo.set_domain_scanned(conn, "alpha", last, "partial", "h", 3600)
    matched = {1: "2026-11-01T00:00:00Z", 2: "2026-11-06T00:00:00Z", 3: "2026-11-01T00:00:00Z",
               4: "2026-11-01T00:00:00Z", 5: "2026-11-06T00:00:00Z"}
    for rid, at in matched.items():
        repo.upsert_repo(conn, hit(rid), at)
        repo.link_domain_repo(conn, "alpha", rid, "q", at)
    repo.mark_repo_gone(conn, 5)
    for rid in (1, 2, 3, 5):  # 4 was never snapshotted
        repo.insert_snapshot(conn, collect_snapshot(rid))
    conn.execute(
        "INSERT INTO verdicts (repo_id, domain_id, model, questions_hash, input_hash, accepted, decided_at) "
        "VALUES (3, 'alpha', 'm', 'q', 'i', 1, ?)", (last,))
    conn.commit()
    fakes = Fakes(monkeypatch)
    collect_domains(None, conn, [domain()], CFG, NOW)
    # 1: snapshotted after its last match -> skipped | 2: matched after its snapshot | 3: accepted | 4: never snapshotted | 5: gone
    assert fakes.enriched[0]["ids"] == [2, 3, 4]
    assert fakes.enriched[0]["slow_for"] == {3}
    assert fakes.enriched[0]["now"] == NOW


def test_enrich_targets_throttle_accepted_by_refresh_every(conn):
    from repka.collect import _enrich_targets

    for rid, age_h in ((1, 30), (2, 10)):  # both accepted, matched before their snapshot
        repo.upsert_repo(conn, hit(rid), "2026-11-01T00:00:00Z")
        repo.link_domain_repo(conn, "alpha", rid, "q", "2026-11-01T00:00:00Z")
        snap = collect_snapshot(rid)
        repo.insert_snapshot(conn, snap.__class__(**{**snap.__dict__, "taken_at": NOW - timedelta(hours=age_h)}))
        conn.execute(
            "INSERT INTO verdicts (repo_id, domain_id, model, questions_hash, input_hash, accepted, decided_at) "
            "VALUES (?, 'alpha', 'm', 'q', 'i', 1, ?)", (rid, "2026-11-01T00:00:00Z"))
    conn.commit()
    assert _enrich_targets(conn, "alpha", NOW, timedelta(0))[0] == [1, 2]  # no throttling
    assert _enrich_targets(conn, "alpha", NOW, timedelta(hours=20))[0] == [1]  # 2 is fresher than 20h
    assert _enrich_targets(conn, "alpha")[0] == [1, 2]  # legacy call without a clock


def collect_snapshot(repo_id):
    from repka.models import Snapshot

    return Snapshot(repo_id, NOW - timedelta(days=3), 1, 0, 0, 0, None, 0, 0, 0, 0, None, None)


# ---- CLI ---------------------------------------------------------------------------------------


def write_domain(home, domain_id="alpha", enabled="true"):
    (home / "domains").mkdir(exist_ok=True)
    (home / "domains" / f"{domain_id}.yaml").write_text(
        f"id: {domain_id}\nname: {domain_id}\nenabled: {enabled}\nprofile: p\nsearch:\n  topics: [mcp]\n"
    )


def test_dry_run_makes_no_client(home, monkeypatch, capsys):
    write_domain(home)

    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not build a client")

    monkeypatch.setattr(collect_cli, "make_client", boom)
    assert main(["collect", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert plan["domains"][0]["domain"] == "alpha"
    assert plan["domains"][0]["scan"] == "first"
    assert plan["domains"][0]["queries"] >= 1
    assert not (home / ".state" / "repka.db").exists()  # dry-run writes nothing


def test_collect_cli_without_token_exits_2(home, monkeypatch, capsys):
    write_domain(home)

    def no_gh(*args, **kwargs):
        raise OSError("no gh")

    monkeypatch.setattr("repka.config.subprocess.run", no_gh)  # no `gh auth token` fallback
    assert main(["collect"]) == 2
    assert "no GitHub token" in capsys.readouterr().err


def test_disabled_domain_with_flag_is_config_error(home, capsys):
    write_domain(home, enabled="false")
    assert main(["collect", "--domain", "alpha"]) == 2
    err = capsys.readouterr().err
    assert "disabled" in err and "enabled: true" in err


def test_collect_cli_github_error_in_a_domain_exits_1_rate_limit_exits_0(home, monkeypatch, capsys):
    write_domain(home)

    class FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(collect_cli, "make_client", lambda cfg, conn: FakeClient())

    def bad_gateway(conn, d, now):
        raise GitHubError("HTTP 502")

    Fakes(monkeypatch, search=bad_gateway)
    assert main(["collect"]) == 1  # CLAUDE.md: GitHub failures are exit 1; the summary is still printed
    assert "GitHub error: HTTP 502" in capsys.readouterr().out
    Fakes(monkeypatch, search=raise_rate_limited)
    assert main(["collect", "--domain", "alpha"]) == 0  # a rate limit is a clean stop, documented as exit 0
    assert "rate limited" in capsys.readouterr().out


def test_collect_cli_summary_and_second_run_not_due(home, monkeypatch, capsys):
    write_domain(home)
    Fakes(monkeypatch)

    class FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(collect_cli, "make_client", lambda cfg, conn: FakeClient())
    assert main(["collect", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["domains"][0]["domain_id"] == "alpha"
    assert main(["collect"]) == 0  # second immediate run
    out = capsys.readouterr().out
    assert "nothing due" in out and "not due: alpha" in out


def test_collect_does_nothing_while_another_run_holds_the_lock(home, monkeypatch, capsys):
    monkeypatch.setattr(collect_cli, "_select", lambda _args: ([], False))
    monkeypatch.setattr(collect_cli, "_collect_locked", lambda *a: pytest.fail("ran without the lock"))
    with RunLock(state_dir() / "run.lock"):
        assert main(["collect"]) == 0
    assert "another repka run is in progress" in capsys.readouterr().err
