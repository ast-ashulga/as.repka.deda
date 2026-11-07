import dataclasses
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from repka.config import (
    ConfigError,
    github_token,
    github_token_source,
    load_config,
    parse_duration,
    project_root,
    worker_env,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_defaults_all_flags_false(home):
    cfg = load_config(home / "missing.yaml")  # missing file -> all defaults
    for section in dataclasses.fields(cfg):
        obj = getattr(cfg, section.name)
        for f in dataclasses.fields(obj):
            if isinstance(getattr(obj, f.name), bool):
                assert getattr(obj, f.name) is False, f"{section.name}.{f.name}"


def test_unknown_key_rejected(home):
    (home / ".config.yaml").write_text("github:\n  per_pag: 1\n")
    with pytest.raises(ConfigError, match="github.per_pag"):
        load_config()


def test_bool_must_be_yaml_bool(home):
    (home / ".config.yaml").write_text('notify:\n  enabled: "yes"\n')
    with pytest.raises(ConfigError):
        load_config()


def test_parse_duration():
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("12h") == timedelta(hours=12)
    assert parse_duration("7d") == timedelta(days=7)
    for bad in ("5x", ""):
        with pytest.raises(ConfigError):
            parse_duration(bad)


def test_project_root_env_override(home):
    assert project_root() == home


def test_worker_env_has_no_secrets(monkeypatch):
    secrets = {"REPKA_GITHUB_TOKEN": "ghp_secret", "REPKA_TG_API_ID": "12345", "REPKA_TG_API_HASH": "hashsecret"}
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    env = worker_env()
    assert not set(secrets) & set(env)
    assert not set(secrets.values()) & set(env.values())


def test_tracked_config_is_valid():
    load_config(REPO_ROOT / ".config.yaml")


def test_github_token_from_env_wins(home, monkeypatch):
    (home / ".env").write_text("REPKA_GITHUB_TOKEN=from_env\n")

    def gh_must_not_run(*args, **kwargs):
        raise AssertionError("gh must not be called when .env has a token")

    monkeypatch.setattr(subprocess, "run", gh_must_not_run)
    assert github_token() == "from_env"


def test_github_token_falls_back_to_gh(home, monkeypatch):
    def fake_run(args, **kwargs):
        assert args == ["gh", "auth", "token"]
        return subprocess.CompletedProcess(args, 0, stdout="from_gh\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert github_token() == "from_gh"


def test_github_token_missing_is_config_error(home, monkeypatch):
    def gh_missing(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", gh_missing)
    with pytest.raises(ConfigError, match="REPKA_GITHUB_TOKEN"):
        github_token()


def test_github_token_source(home, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="x", stderr=""))
    assert github_token_source() == "gh"
    (home / ".env").write_text("REPKA_GITHUB_TOKEN=from_env\n")
    assert github_token_source() == "env"
