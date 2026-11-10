"""Paths, strict `.config.yaml` loading, `.env` secrets and subprocess environments.

Every key is optional and defaults to architecture § 3.2; every boolean defaults to False.
Unknown keys and wrong types raise ConfigError naming the dotted key path.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from dotenv import dotenv_values


class ConfigError(ValueError):
    """A problem the user can fix: bad config, bad duration, missing token. Exit code 2."""


def project_root() -> Path:
    """The repka home: $REPKA_HOME if set, otherwise the checkout that contains src/."""
    override = os.environ.get("REPKA_HOME")
    if override:
        return Path(override).expanduser().absolute()
    return Path(__file__).resolve().parents[2]


def state_dir() -> Path:
    return project_root() / ".state"


def db_path() -> Path:
    return state_dir() / "repka.db"


def logs_dir() -> Path:
    return project_root() / "logs"


def domains_dir() -> Path:
    return project_root() / "domains"


def deciders_dir() -> Path:
    return project_root() / "deciders"


_DURATION = re.compile(r"(\d+)([mhd])")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: str) -> timedelta:
    """'30m', '12h' or '7d' -> timedelta. Anything else is a ConfigError."""
    match = _DURATION.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None or int(match.group(1)) == 0:
        raise ConfigError(f"bad duration {value!r}: use a number and m, h or d (e.g. 30m, 12h, 7d)")
    return timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})


@dataclass(frozen=True)
class GithubConfig:
    search_per_minute: int = 25  # client throttle for search endpoints (GitHub max 30)
    per_page: int = 100
    timeout_s: int = 30


@dataclass(frozen=True)
class CollectConfig:
    first_window: timedelta = timedelta(days=365)  # backfill window on a domain's first scan (pushed:>=)
    overlap: timedelta = timedelta(days=1)  # incremental scans use pushed:>= last_scan - overlap
    max_repos_per_domain_scan: int = 3000
    enrich_batch: int = 50  # GraphQL nodes per query
    slow_metrics_every: timedelta = timedelta(days=7)  # contributors + participation refresh period
    # an accepted repo that the scan's search did not match is re-snapshotted only when its latest snapshot is at least
    # this old; 0 = every scan (no throttling)
    accepted_refresh_every: timedelta = timedelta(0)


@dataclass(frozen=True)
class TriageConfig:
    enabled: bool = False
    model: str = "d1-3B"  # name in deciders/models.yaml
    deciders_dir: str = "deciders"
    timeout_s: int = 1800
    online: bool = False  # allow HF downloads inside workers
    allow_remote: bool = False  # permit remote decider backends; never default
    readme_chars: int = 4000
    thresholds: dict[str, float] = field(default_factory=lambda: {"relevant": 0.70, "substance": 2.0})


@dataclass(frozen=True)
class EmbedConfig:
    enabled: bool = False
    backend: str = "ollama"
    model: str = "nomic-embed-text"
    url: str = "http://localhost:11434"
    batch: int = 32


@dataclass(frozen=True)
class DigestConfig:
    slots: list[str] = field(default_factory=lambda: ["08:30", "19:30"])  # local time
    max_lateness: timedelta = timedelta(hours=6)
    max_items_per_domain: int = 10
    new_max_age: timedelta = timedelta(days=90)  # a first-seen repo created within this age is "new"
    star_spike_min_delta: int = 50  # stars_delta_1d threshold for the "top movers" section
    insights: bool = False  # digest insights section (off until the feature ships)
    releases_message: bool = False  # separate releases message at releases_slot
    releases_slot: str = "09:00"  # local time HH:MM for the releases message
    facet_min_p: float = 0.7  # minimum probability for a facet to be shown in insights
    pending_max_age: timedelta = timedelta(days=7)  # how long a pending insight waits before it expires


@dataclass(frozen=True)
class NotifyConfig:
    enabled: bool = False
    backend: str = "telegram_user"  # own account via fast-mcp-telegram (ADR-005)
    chat_id: str = "me"  # "me" = Saved Messages; or numeric id / @username
    topic_id: int | None = None


@dataclass(frozen=True)
class RunConfig:
    send: bool = False  # digests are sent only if notify.enabled AND run.send (or --send)
    log_level: str = "INFO"
    progress: str = "plain"  # stderr progress while `repka run` works: plain (legacy lines) | live | lines | jsonl


@dataclass(frozen=True)
class AppConfig:
    github: GithubConfig = field(default_factory=GithubConfig)
    collect: CollectConfig = field(default_factory=CollectConfig)
    triage: TriageConfig = field(default_factory=TriageConfig)
    embed: EmbedConfig = field(default_factory=EmbedConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    run: RunConfig = field(default_factory=RunConfig)


Parser = Callable[[str, Any], Any]


def _bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false (got {value!r})")
    return value


def _int(key: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{key} must be a positive integer (got {value!r})")
    return value


def _optional_int(key: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer or null (got {value!r})")
    return value


def _number(key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number (got {value!r})")
    return float(value)


def _str(key: str, value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)  # e.g. a numeric chat id written without quotes
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string (got {value!r})")
    return value.strip()


def _slot(key: str, value: Any) -> str:
    """A local time of day written HH:MM (zero-padded), e.g. 09:00."""
    if not isinstance(value, str) or len(value) != 5:
        raise ConfigError(f"{key} must be a local time HH:MM (got {value!r})")
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        raise ConfigError(f"{key} must be a local time HH:MM (got {value!r})") from None
    return value


def _probability(key: str, value: Any) -> float:
    p = _number(key, value)
    if not 0 < p <= 1:
        raise ConfigError(f"{key} must be above 0 and at most 1 (got {value!r})")
    return p


def _duration(key: str, value: Any) -> timedelta:
    try:
        return parse_duration(value)
    except ConfigError as exc:
        raise ConfigError(f"{key}: {exc}") from None


def _choice(*options: str) -> Parser:
    def check(key: str, value: Any) -> str:
        if value not in options:
            raise ConfigError(f"{key} must be one of {', '.join(options)} (got {value!r})")
        return value

    return check


def _str_list(key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{key} must be a list of non-empty strings (got {value!r})")
    return list(value)


def _thresholds(key: str, value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must map question ids to numbers (got {value!r})")
    return {str(qid): _number(f"{key}.{qid}", v) for qid, v in value.items()}


T = TypeVar("T")


def _load(section: str, raw: Any, cls: type[T], parsers: dict[str, Parser]) -> T:
    """Build `cls` from one YAML section; `parsers` is the complete list of allowed keys."""
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"'{section}' must be a mapping of keys (got {raw!r})")
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        if name not in parsers:
            raise ConfigError(f"unknown config key '{section}.{name}'")
        kwargs[name] = parsers[name](f"{section}.{name}", value)
    return cls(**kwargs)


def load_github(raw: Any) -> GithubConfig:
    return _load(
        "github",
        raw,
        GithubConfig,
        {"search_per_minute": _int, "per_page": _int, "timeout_s": _int},
    )


def load_collect(raw: Any) -> CollectConfig:
    return _load(
        "collect",
        raw,
        CollectConfig,
        {
            "first_window": _duration,
            "overlap": _duration,
            "max_repos_per_domain_scan": _int,
            "enrich_batch": _int,
            "slow_metrics_every": _duration,
            "accepted_refresh_every": _duration,
        },
    )


def load_triage(raw: Any) -> TriageConfig:
    return _load(
        "triage",
        raw,
        TriageConfig,
        {
            "enabled": _bool,
            "model": _str,
            "deciders_dir": _str,
            "timeout_s": _int,
            "online": _bool,
            "allow_remote": _bool,
            "readme_chars": _int,
            "thresholds": _thresholds,
        },
    )


def load_embed(raw: Any) -> EmbedConfig:
    return _load(
        "embed",
        raw,
        EmbedConfig,
        {"enabled": _bool, "backend": _str, "model": _str, "url": _str, "batch": _int},
    )


def load_digest(raw: Any) -> DigestConfig:
    return _load(
        "digest",
        raw,
        DigestConfig,
        {
            "slots": _str_list,
            "max_lateness": _duration,
            "max_items_per_domain": _int,
            "new_max_age": _duration,
            "star_spike_min_delta": _int,
            "insights": _bool,
            "releases_message": _bool,
            "releases_slot": _slot,
            "facet_min_p": _probability,
            "pending_max_age": _duration,
        },
    )


def load_notify(raw: Any) -> NotifyConfig:
    return _load(
        "notify",
        raw,
        NotifyConfig,
        {"enabled": _bool, "backend": _str, "chat_id": _str, "topic_id": _optional_int},
    )


def load_run(raw: Any) -> RunConfig:
    return _load(
        "run",
        raw,
        RunConfig,
        {
            "send": _bool,
            "log_level": _choice("DEBUG", "INFO", "WARNING", "ERROR"),
            "progress": _choice("plain", "live", "lines", "jsonl"),
        },
    )


_LOADERS: dict[str, Callable[[Any], Any]] = {
    "github": load_github,
    "collect": load_collect,
    "triage": load_triage,
    "embed": load_embed,
    "digest": load_digest,
    "notify": load_notify,
    "run": load_run,
}


def load_config(path: Path | None = None) -> AppConfig:
    """Load and validate `.config.yaml` (default: <repka home>/.config.yaml). Missing file -> defaults."""
    if path is None:
        path = project_root() / ".config.yaml"
    raw: Any = {}
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path.name} is not valid YAML: {exc}") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} must be a mapping of sections")
    for name in raw:
        if name not in _LOADERS:
            raise ConfigError(f"unknown config key '{name}' in {path.name}")
    return AppConfig(**{name: loader(raw.get(name)) for name, loader in _LOADERS.items()})


def read_env() -> dict[str, str]:
    """Values from <repka home>/.env. Read only; never exported to os.environ. Missing file -> {}."""
    path = project_root() / ".env"
    if not path.exists():
        return {}
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def _env_token() -> str:
    return read_env().get("REPKA_GITHUB_TOKEN", "").strip()


def github_token() -> str:
    """REPKA_GITHUB_TOKEN from .env, else the token of `gh auth token`. The value is never logged."""
    token = _env_token()
    if token:
        return token
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        proc = None
    token = (proc.stdout or "").strip() if proc is not None and proc.returncode == 0 else ""
    if not token:
        raise ConfigError(
            "no GitHub token: set REPKA_GITHUB_TOKEN in .env, or log in with `gh auth login` "
            "(repka falls back to `gh auth token`)"
        )
    return token


def github_token_source() -> Literal["env", "gh"]:
    """Where github_token() gets its value: "env" when .env holds one, otherwise "gh"."""
    return "env" if _env_token() else "gh"


_WORKER_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "HF_HOME", "HF_HUB_CACHE")


def worker_env() -> dict[str, str]:
    """Minimal environment for decider/embedder subprocesses. Never contains tokens."""
    return {key: os.environ[key] for key in _WORKER_ENV_KEYS if key in os.environ}


# --- Telegram (user account via fast-mcp-telegram) ---------------------------------------------

_SESSION_NAME = "telegram"
_SERVER_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


@dataclass(frozen=True)
class TelegramCredentials:
    api_id: str
    api_hash: str

    def __repr__(self) -> str:  # never leak secrets into logs / tracebacks
        return "TelegramCredentials(api_id=<hidden>, api_hash=<hidden>)"


def telegram_credentials() -> TelegramCredentials:
    """REPKA_TG_API_ID / REPKA_TG_API_HASH from .env. ConfigError names the missing keys, never values."""
    env = read_env()
    values = {key: env.get(key, "").strip() for key in ("REPKA_TG_API_ID", "REPKA_TG_API_HASH")}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ConfigError(
            f"missing in .env: {', '.join(missing)} (Telegram app credentials from https://my.telegram.org)"
        )
    return TelegramCredentials(api_id=values["REPKA_TG_API_ID"], api_hash=values["REPKA_TG_API_HASH"])


def session_dir() -> Path:
    """<root>/.sessions, owner-only: the Telegram session inside is full access to the account."""
    path = project_root() / ".sessions"
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def session_file_path() -> Path:
    """Where the Telegram session lives. Read-only: creates nothing (preflight/doctor use this)."""
    return project_root() / ".sessions" / f"{_SESSION_NAME}.session"


def session_file() -> Path:
    session_dir()  # makes sure the owner-only directory exists
    return session_file_path()


def require_session() -> Path:
    path = session_file()
    if not path.exists():
        raise ConfigError(
            f"no Telegram session at {path}; log in once (interactive: phone number, code, 2FA): "
            "uv run repka notify login"
        )
    return path


def telegram_server_env() -> dict[str, str]:
    """Explicit, minimal environment for the fast-mcp-telegram subprocess. Never contains a GitHub token."""
    creds = telegram_credentials()
    env = {key: os.environ[key] for key in _SERVER_ENV_KEYS if key in os.environ}
    env.update(
        API_ID=creds.api_id,
        API_HASH=creds.api_hash,
        SESSION_DIR=str(session_dir()),
        SESSION_NAME=_SESSION_NAME,
        SERVER_MODE="stdio",
        DO_NOT_TRACK="1",  # opt out of the server's anonymous telemetry
    )
    return env


def venv_script(name: str) -> str:
    """Path of a console script from this project's own virtualenv."""
    path = Path(sys.executable).parent / name
    if not path.exists():
        raise ConfigError(f"{name} not found next to {sys.executable}; run `uv sync`")
    return str(path)
