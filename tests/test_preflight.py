import json
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from repka import config, pipeline, preflight
from repka.cli import main
from repka.config import (
    AppConfig,
    DigestConfig,
    EmbedConfig,
    NotifyConfig,
    TriageConfig,
)
from repka.github.client import AuthError, AuthInfo, GitHubError

NOW = datetime(2026, 11, 8, 8, 0, 0, tzinfo=UTC)
REV = "a" * 40
QUESTIONS = """\
questions:
  relevant: {type: noul, instructions: relevant}
  substance: {type: score, criteria: [none, some, lots]}
state_template: "{name}"
"""
MODELS = f"""\
models:
  d1-3B: {{runner: d1_worker, venv: .venv-liquid, repo: LiquidAI/d1-3B, revision: {REV}}}
"""
TRIAGE_ON = AppConfig(triage=TriageConfig(enabled=True))


def ids(results, status=None):
    return [r.id for r in results if status is None or r.status == status]


def by_id(results, rid):
    return next(r for r in results if r.id == rid)


@pytest.fixture(autouse=True)
def isolated(home, monkeypatch):
    """No real gh login, Ollama or Hugging Face cache can leak into a test."""

    def no_gh(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="not logged in")

    monkeypatch.setattr(config.subprocess, "run", no_gh)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    monkeypatch.setenv("HF_HUB_CACHE", str(home / "hf-hub"))


def write_token(home):
    (home / ".env").write_text("REPKA_GITHUB_TOKEN=ghp_test\n")


def write_domain(home, enabled="true"):
    (home / "domains").mkdir(exist_ok=True)
    (home / "domains" / "alpha.yaml").write_text(
        f"id: alpha\nname: alpha\nenabled: {enabled}\nprofile: p\nsearch:\n  topics: [mcp]\n"
    )


def write_deciders(home):
    (home / "deciders").mkdir(exist_ok=True)
    (home / "deciders" / "questions.yaml").write_text(QUESTIONS)
    (home / "deciders" / "models.yaml").write_text(MODELS)


def probe_returning(expires_at=None):
    return lambda *a: AuthInfo(login="octocat", expires_at=expires_at)


def probe_raising(exc):
    def probe(*a):
        raise exc

    return probe


def test_no_github_token_is_error_with_fix(home):
    results = preflight.diagnose(AppConfig(), send=False, auth_probe=probe_returning(), now=NOW)
    token = by_id(results, "github-token")
    assert token.status == "error" and "gh auth login" in token.fix and ".env" in token.fix


@pytest.mark.parametrize("source", ["env", "gh"])
def test_rejected_token_is_error_with_fix(home, monkeypatch, source):
    if source == "env":
        write_token(home)
    else:
        monkeypatch.setattr(
            config.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="gho_x\n", stderr="")
        )
    results = preflight.diagnose(AppConfig(), send=False, auth_probe=probe_raising(AuthError("401")), now=NOW)
    auth = by_id(results, "github-auth")
    assert auth.status == "error" and "expired, revoked or invalid" in auth.title
    if source == "env":
        assert "settings/tokens" in auth.fix and "REPKA_GITHUB_TOKEN" in auth.fix
    else:
        assert "gh auth login" in auth.fix
    errors = [f for f in preflight.check(AppConfig(), send=False, auth_probe=probe_raising(AuthError("401")), now=NOW) if f.level == "error"]
    assert any("expired, revoked or invalid" in f.title for f in errors)


def test_token_expiring_soon_is_warning(home):
    write_token(home)
    soon = NOW + timedelta(days=3)
    auth = by_id(preflight.diagnose(AppConfig(), send=False, auth_probe=probe_returning(soon), now=NOW), "github-auth")
    assert auth.status == "warn" and "expires on 2026-11-11" in auth.title and "settings/tokens" in auth.fix
    later = by_id(preflight.diagnose(AppConfig(), send=False, auth_probe=probe_returning(NOW + timedelta(days=30)), now=NOW), "github-auth")
    assert later.status == "ok"


@pytest.mark.parametrize("failure", [GitHubError("boom"), httpx.ConnectError("down")])
def test_probe_network_failure_is_warning(home, failure):
    write_token(home)
    results = preflight.diagnose(AppConfig(), send=False, auth_probe=probe_raising(failure), now=NOW)
    auth = by_id(results, "github-auth")
    assert auth.status == "warn" and "could not verify the GitHub token (offline?)" in auth.title
    assert "github-token" in ids(results, "ok")  # the run continues: only a warning


def test_probe_skipped_when_no_token(home):
    calls = []
    results = preflight.diagnose(AppConfig(), send=False, auth_probe=lambda: calls.append(1), now=NOW)
    assert calls == [] and "github-auth" not in ids(results)


def test_no_enabled_domain_is_error(home):
    write_domain(home, enabled="false")
    domains = by_id(preflight.diagnose(AppConfig(), send=False, now=NOW), "domains")
    assert domains.status == "error" and "domain new" in domains.fix and "enabled: true" in domains.fix
    write_domain(home)
    assert by_id(preflight.diagnose(AppConfig(), send=False, now=NOW), "domains").status == "ok"


def test_triage_missing_venv_and_weights(home):
    write_deciders(home)
    results = preflight.diagnose(TRIAGE_ON, send=False, now=NOW)
    assert by_id(results, "triage-venv").fix == "./deciders/setup.sh"
    weights = by_id(results, "triage-weights")
    assert weights.status == "error" and weights.fix == "./deciders/setup.sh --download d1-3B"
    assert by_id(results, "triage-thresholds").status == "ok"
    (home / "deciders" / ".venv-liquid" / "bin").mkdir(parents=True)
    (home / "deciders" / ".venv-liquid" / "bin" / "python").write_text("")
    snapshot = home / "hf-hub" / "models--LiquidAI--d1-3B" / "snapshots" / REV
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").write_text("w")
    results = preflight.diagnose(TRIAGE_ON, send=False, now=NOW)
    assert {"triage-venv", "triage-weights"} <= set(ids(results, "ok"))


def test_code_only_snapshot_counts_as_missing_weights(home):
    write_deciders(home)
    snapshot = home / "hf-hub" / "models--LiquidAI--d1-3B" / "snapshots" / REV
    snapshot.mkdir(parents=True)
    for name in ("config.json", "modeling.py", "README.md"):
        (snapshot / name).write_text("x")
    weights = by_id(preflight.diagnose(TRIAGE_ON, send=False, now=NOW), "triage-weights")
    assert weights.status == "error" and "no weights" in weights.title


def test_triage_unknown_model_and_bad_thresholds(home):
    write_deciders(home)
    unknown = AppConfig(triage=TriageConfig(enabled=True, model="nope"))
    assert by_id(preflight.diagnose(unknown, send=False, now=NOW), "triage-model").status == "error"
    bad = AppConfig(triage=TriageConfig(enabled=True, thresholds={"ghost": 1.0}))
    assert by_id(preflight.diagnose(bad, send=False, now=NOW), "triage-thresholds").status == "error"


def test_send_without_tg_credentials_session_or_chat(home):
    cfg = AppConfig(notify=NotifyConfig(enabled=True, chat_id=""))
    results = preflight.diagnose(cfg, send=True, now=NOW)
    assert "REPKA_TG_API_ID" in by_id(results, "telegram-credentials").title
    assert "notify login" in by_id(results, "telegram-session").fix
    assert by_id(results, "telegram-chat").status == "error"
    assert not (home / ".sessions").exists()  # read-only: the check creates nothing
    (home / ".env").write_text("REPKA_TG_API_ID=1\nREPKA_TG_API_HASH=h\n")
    (home / ".sessions").mkdir()
    (home / ".sessions" / "telegram.session").write_text("s")
    ok = preflight.diagnose(AppConfig(notify=NotifyConfig(enabled=True)), send=True, now=NOW)
    assert {"telegram-credentials", "telegram-session", "telegram-chat"} <= set(ids(ok, "ok"))
    assert by_id(preflight.diagnose(AppConfig(), send=True, now=NOW), "send").status == "error"  # notify.enabled is false


def test_flags_off_produce_warnings_only(home, monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)  # ollama missing, whatever this machine has
    write_token(home)
    write_domain(home)
    cfg = AppConfig(embed=EmbedConfig(enabled=True))
    findings = preflight.check(cfg, send=False, auth_probe=probe_returning(), now=NOW)
    assert [f.level for f in findings] == ["warning"] * 3  # triage off, ollama missing, preview only
    titles = " | ".join(f.title for f in findings)
    assert "digests will stay empty" in titles and "preview only" in titles and "ollama" in titles
    assert preflight.check(AppConfig(), send=False, auth_probe=probe_returning(), now=NOW)[0].level == "warning"


@pytest.mark.parametrize(
    "insights, releases_message, warned",
    [(True, False, True), (False, True, True), (False, False, False), (True, True, False)],
)
def test_preflight_warns_when_only_one_digest_flag_is_on(insights, releases_message, warned):
    cfg = AppConfig(digest=DigestConfig(insights=insights, releases_message=releases_message))
    results = preflight.check_digest_flags(cfg)
    if warned:
        assert [(r.id, r.status) for r in results] == [("digest-flags", "warn")]
        assert results[0].fix
    else:
        assert results == []


def test_findings_list_puts_errors_first_and_is_numbered(home):
    findings = preflight.check(AppConfig(), send=False, auth_probe=probe_returning(), now=NOW)
    assert findings[0].level == "error" and findings[-1].level == "warning"
    text = preflight.format_findings(findings)
    assert text.startswith("1. [error]") and "\n   fix: " in text


def test_doctor_exit_codes(home, monkeypatch, capsys):
    monkeypatch.setattr(preflight, "live_auth_probe", probe_returning())
    assert main(["doctor"]) == 2  # no token, no domain
    out = capsys.readouterr().out
    assert "error  github-token" in out and "-> fix: run `gh auth login`" in out and "To do:" in out
    write_token(home)
    write_domain(home)
    assert main(["doctor"]) == 0  # warnings (triage off, preview only) do not fail
    out = capsys.readouterr().out
    assert "ok     github-auth" in out and "warn   triage" in out and "0 error(s), 2 warning(s)" in out
    assert main(["doctor", "--send"]) == 2  # notify is not enabled


def test_doctor_json(home, monkeypatch, capsys):
    write_token(home)
    write_domain(home)
    monkeypatch.setattr(preflight, "live_auth_probe", probe_returning())
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["errors"] == 0 and payload["warnings"] == 2
    assert {"id": "domains", "status": "ok", "title": "1 enabled: alpha", "fix": ""} in payload["checks"]
    monkeypatch.setattr(preflight, "live_auth_probe", probe_raising(AuthError("401")))
    assert main(["doctor", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and next(c for c in payload["checks"] if c["id"] == "github-auth")["status"] == "error"


def test_run_stops_on_errors_before_lock(home, monkeypatch, capsys):
    write_domain(home)  # no token: preflight error
    monkeypatch.setattr(pipeline, "build_boundaries", lambda *a, **k: pytest.fail("must stop before any work"))
    assert main(["run"]) == 2
    err = capsys.readouterr().err
    assert "[error] no GitHub token" in err and "repka run did not start" in err
    assert not (home / ".state").exists()  # no lock file, no database


def test_run_stops_on_rejected_token_before_any_search(home, monkeypatch, capsys):
    write_token(home)
    write_domain(home)
    monkeypatch.setattr(preflight, "live_auth_probe", probe_raising(AuthError("401")))
    monkeypatch.setattr(pipeline, "build_boundaries", lambda *a, **k: pytest.fail("must stop before any search"))
    assert main(["run"]) == 2
    assert "expired, revoked or invalid" in capsys.readouterr().err


def test_embed_model_checked_against_ollama_tags(home, monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/ollama")
    cfg = AppConfig(embed=EmbedConfig(enabled=True, model="nomic-embed-text"))

    def model_check(probe):
        return by_id(preflight.diagnose(cfg, send=False, auth_probe=probe_returning(), now=NOW, models_probe=probe), "embed-model")

    assert model_check(lambda: ["nomic-embed-text:latest"]).status == "ok"
    missing = model_check(lambda: ["llama3:8b"])
    assert missing.status == "warn" and "ollama pull nomic-embed-text" in missing.fix
    assert model_check(probe_raising(httpx.ConnectError("down"))).status == "warn"


def _digests_db(home, *rows):
    from repka.store.db import connect, migrate

    conn = connect()
    migrate(conn)
    for slot, status in rows:
        conn.execute("INSERT INTO digests (slot, local_date, status) VALUES (?, '2026-11-08', ?)", (slot, status))
    conn.commit()
    conn.close()


def test_digest_claims_check_is_silent_without_a_database(home):
    assert preflight.check_digest_claims(config.db_path()) == []


def test_digest_claims_check_warns_about_sending_and_unknown_rows(home):
    _digests_db(home, ("08:30", "sent"), ("19:30", "unknown"), ("releases@09:00", "sending"))
    results = preflight.check_digest_claims(config.db_path())
    assert [r.status for r in results] == ["warn", "warn"]
    assert "19:30" in results[0].title and "2026-11-08" in results[0].title and "'unknown'" in results[0].title
    assert "releases@09:00" in results[1].title and "'sending'" in results[1].title
    assert "DELETE FROM digests WHERE id = 2" in results[0].fix and "Telegram" in results[0].fix


def test_digest_claims_check_ok_when_nothing_is_stuck(home):
    _digests_db(home, ("08:30", "sent"))
    assert [r.status for r in preflight.check_digest_claims(config.db_path())] == ["ok"]


def test_doctor_shows_a_stuck_claim_as_a_warning_without_failing(home, monkeypatch, capsys):
    write_token(home)
    write_domain(home)
    monkeypatch.setattr(preflight, "live_auth_probe", probe_returning())
    _digests_db(home, ("19:30", "unknown"))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "warn   digest-claims" in out and "19:30" in out and "DELETE FROM digests" in out
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == 0 and payload["warnings"] == 3
    assert next(c for c in payload["checks"] if c["id"] == "digest-claims")["status"] == "warn"
