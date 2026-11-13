import dataclasses
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from repka import config as config_module
from repka.config import (
    ConfigError,
    github_token,
    github_token_source,
    load_collect,
    load_config,
    load_digest,
    load_embed,
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


def test_project_root_is_the_checkout_without_override(monkeypatch):
    monkeypatch.delenv("REPKA_HOME", raising=False)
    assert project_root() == REPO_ROOT


def test_project_root_outside_a_checkout_is_a_clear_error(monkeypatch, tmp_path):
    # a non-editable install: the package sits in <prefix>/lib/pythonX/site-packages/repka, no pyproject.toml above it
    fake = tmp_path / "lib" / "site-packages" / "repka" / "config.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("")
    monkeypatch.delenv("REPKA_HOME", raising=False)
    monkeypatch.setattr(config_module, "__file__", str(fake))
    with pytest.raises(ConfigError, match="REPKA_HOME"):
        project_root()


def test_worker_env_has_no_secrets(monkeypatch):
    secrets = {"REPKA_GITHUB_TOKEN": "ghp_secret", "REPKA_TG_API_ID": "12345", "REPKA_TG_API_HASH": "hashsecret"}
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    env = worker_env()
    assert not set(secrets) & set(env)
    assert not set(secrets.values()) & set(env.values())


def test_tracked_config_is_valid():
    load_config(REPO_ROOT / ".config.yaml")


def test_tracked_config_equals_the_code_defaults():
    """The shipped .config.yaml says its values are the defaults: every boolean false, every value equal."""
    tracked = load_config(REPO_ROOT / ".config.yaml")
    defaults = load_config(REPO_ROOT / "does-not-exist.yaml")
    for section in dataclasses.fields(defaults):
        got, want = getattr(tracked, section.name), getattr(defaults, section.name)
        for f in dataclasses.fields(want):
            value = getattr(got, f.name)
            if isinstance(value, bool):
                assert value is False, f"{section.name}.{f.name} must be false in the tracked .config.yaml"
            assert value == getattr(want, f.name), f"{section.name}.{f.name} differs from the code default"
    assert tracked == defaults


def test_digest_slots_are_validated():
    assert load_digest({"slots": ["06:00", "18:45"]}).slots == ["06:00", "18:45"]
    for bad in (["9am"], ["25:00"], ["9:00"], [900], "09:00"):
        with pytest.raises(ConfigError, match=r"digest\.slots"):
            load_digest({"slots": bad})


def test_accepted_refresh_every_accepts_zero():
    assert load_collect({"accepted_refresh_every": "0s"}).accepted_refresh_every == timedelta(0)
    for zero in (0, "0", "0s", " 0s "):
        assert load_collect({"accepted_refresh_every": zero}).accepted_refresh_every == timedelta(0)
    for bad in (False, 0.0, "0m", "-1h", 5):
        with pytest.raises(ConfigError, match="collect.accepted_refresh_every"):
            load_collect({"accepted_refresh_every": bad})
    with pytest.raises(ConfigError, match="slow_metrics_every"):  # zero stays invalid for the other durations
        load_collect({"slow_metrics_every": 0})
    assert load_collect({"accepted_refresh_every": "20h"}).accepted_refresh_every == timedelta(hours=20)
    with pytest.raises(ConfigError, match="collect.first_window"):
        load_collect({"first_window": "0s"})


@pytest.mark.parametrize("url", ["http://localhost:11434", "http://127.0.0.1:11434/", "http://[::1]:11434"])
def test_embed_url_loopback_is_accepted(url):
    assert load_embed({"url": url}).url == url


@pytest.mark.parametrize("url", ["http://example.com:11434", "http://192.168.1.5:11434", "http://0.0.0.0:11434", "localhost:11434", "ftp://localhost"])
def test_embed_url_elsewhere_is_rejected(url):
    with pytest.raises(ConfigError, match="embed.url"):
        load_embed({"url": url})


def test_worker_env_passes_both_hf_cache_variables(monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", "/a")
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", "/b")
    env = worker_env()
    assert env["HF_HUB_CACHE"] == "/a" and env["HUGGINGFACE_HUB_CACHE"] == "/b"


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


def test_digest_new_keys_default_off(home):
    cfg = load_config(home / "missing.yaml")
    assert cfg.digest.insights is False
    assert cfg.digest.releases_message is False
    assert cfg.digest.releases_slot == "09:00"
    assert cfg.digest.facet_min_p == 0.7
    assert cfg.digest.pending_max_age == timedelta(days=7)


def test_digest_new_keys_parse():
    d = load_digest({"insights": True, "releases_slot": "07:15", "facet_min_p": 0.5, "pending_max_age": "3d"})
    assert d.insights is True
    assert d.releases_slot == "07:15"
    assert d.facet_min_p == 0.5
    assert d.pending_max_age == timedelta(days=3)


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"releases_slot": "9am"}, "digest.releases_slot must be a local time HH:MM"),
        ({"releases_slot": "25:00"}, "digest.releases_slot must be a local time HH:MM"),
        ({"facet_min_p": 1.5}, "digest.facet_min_p must be above 0 and at most 1"),
        ({"facet_min_p": 0}, "digest.facet_min_p must be above 0 and at most 1"),
        ({"insights": "yes"}, "digest.insights must be true or false"),
        ({"releases_message": 1}, "digest.releases_message must be true or false"),
        ({"pending_max_age": "soon"}, "digest.pending_max_age: bad duration 'soon'"),
    ],
)
def test_digest_bad_values_name_the_key(raw, message):
    with pytest.raises(ConfigError) as exc:
        load_digest(raw)
    assert message in str(exc.value)
    assert "unknown config key" not in str(exc.value)


def test_shipped_domains_never_run_by_accident():
    """A fresh clone must not scan anything: every shipped domain file is valid and disabled."""
    from repka.domain.schema import load_domain, load_domains

    assert not [d.id for d in load_domains(REPO_ROOT / "domains") if d.enabled]
    examples = sorted((REPO_ROOT / "domains" / "examples").glob("*.yaml"))
    assert examples
    for path in examples:
        assert load_domain(path).enabled is False, path.name
