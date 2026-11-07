"""Preflight: everything `repka run` needs, checked up front, listed at once with the exact fix.

`diagnose` returns one `Result` per check (ok, warn or error); `check` keeps only the problems as `Finding`s
(errors first), which is what `repka run` stops on. Reads files and the environment only. The single network
call is one `GET /user` through the injected `auth_probe`; no model, worker or Telegram session is started.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx

from repka import config
from repka.config import AppConfig, ConfigError
from repka.domain.schema import Domain, load_domains
from repka.github.client import FIX_ENV, FIX_GH, AuthError, AuthInfo, GitHubClient, GitHubError
from repka.triage import deciders, triage

EXPIRY_WARNING = timedelta(days=7)
WEIGHT_PATTERNS = ("*.safetensors", "*.bin", "*.gguf", "*.onnx")  # the files fetch.py skips with --code-only

Status = Literal["ok", "warn", "error"]


@dataclass(frozen=True)
class Finding:
    level: Literal["error", "warning"]
    title: str
    fix: str


@dataclass(frozen=True)
class Result:
    """One check: `id` is stable (scripts and --json use it), `fix` is empty when status is ok."""

    id: str
    status: Status
    title: str
    fix: str = ""


def _ok(id: str, title: str) -> Result:
    return Result(id, "ok", title)


def _warn(id: str, title: str, fix: str) -> Result:
    return Result(id, "warn", title, fix)


def _error(id: str, title: str, fix: str) -> Result:
    return Result(id, "error", title, fix)


# --- GitHub token -----------------------------------------------------------------------------------


def live_auth_probe(cfg: AppConfig) -> AuthInfo:
    """The one real network call: GET /user with the configured token. One short attempt: offline must fail fast."""
    client = GitHubClient(
        config.github_token(),
        replace(cfg.github, timeout_s=5),
        token_source=config.github_token_source(),
        max_server_retries=0,
    )
    try:
        return client.check_auth()
    finally:
        client.close()


def _github(cfg: AppConfig, auth_probe: Callable[[], AuthInfo] | None, now: datetime) -> list[Result]:
    try:
        config.github_token()
    except ConfigError:
        return [
            _error(
                "github-token",
                "no GitHub token",
                "run `gh auth login`, or put REPKA_GITHUB_TOKEN=<token> in .env",
            )
        ]
    source = config.github_token_source()
    token_fix = FIX_GH if source == "gh" else FIX_ENV
    results = [_ok("github-token", "token from the gh login" if source == "gh" else "token from .env (REPKA_GITHUB_TOKEN)")]
    probe = auth_probe or (lambda: live_auth_probe(cfg))
    try:
        info = probe()
    except AuthError:  # a ConfigError: must come before the broader handlers below
        results.append(_error("github-auth", "GitHub token expired, revoked or invalid", token_fix))
    except (GitHubError, httpx.HTTPError, OSError):
        results.append(
            _warn("github-auth", "could not verify the GitHub token (offline?)", "check the network; `repka run` retries")
        )
    else:
        if info.expires_at is not None and info.expires_at - now <= EXPIRY_WARNING:
            when = info.expires_at.astimezone(UTC).date().isoformat()
            results.append(_warn("github-auth", f"GitHub token expires on {when}", token_fix))
        else:
            expiry = "does not expire" if info.expires_at is None else f"valid until {info.expires_at:%Y-%m-%d}"
            results.append(_ok("github-auth", f"authenticated as {info.login}, token {expiry}"))
    return results


# --- domains ----------------------------------------------------------------------------------------


def _domains(domains: list[Domain] | ConfigError) -> list[Result]:
    if isinstance(domains, ConfigError):
        return [_error("domains", f"invalid domain file: {domains}", "fix the file named above (see: uv run repka domain show <id>)")]
    enabled = [d.id for d in domains if d.enabled]
    if not enabled:
        return [
            _error(
                "domains",
                "no enabled domain",
                "create one with `uv run repka domain new`, or set `enabled: true` in domains/<id>.yaml",
            )
        ]
    return [_ok("domains", f"{len(enabled)} enabled: {', '.join(enabled)}")]


# --- triage -----------------------------------------------------------------------------------------


def hf_hub_cache() -> Path:
    """The Hugging Face hub cache, resolved like huggingface_hub does (without importing it)."""
    env = os.environ
    explicit = env.get("HF_HUB_CACHE") or env.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    return Path(env.get("HF_HOME") or "~/.cache/huggingface").expanduser() / "hub"


def _has_weights(snapshot: Path) -> bool:
    return snapshot.is_dir() and any(p.exists() for pattern in WEIGHT_PATTERNS for p in snapshot.rglob(pattern))


def _triage(cfg: AppConfig, domains: list[Domain] | ConfigError) -> list[Result]:
    t = cfg.triage
    if not t.enabled:
        return [_warn("triage", "triage is off: digests will stay empty", "set `triage.enabled: true` in .config.yaml")]
    deciders_path = config.project_root() / t.deciders_dir
    results: list[Result] = []
    try:
        models = deciders.load_models(deciders_path / "models.yaml")
        base = deciders.load_questions(deciders_path / "questions.yaml")
    except ConfigError as exc:
        return [_error("triage-model", f"cannot read the decider registry: {exc}", "restore deciders/models.yaml and questions.yaml")]
    spec = models.get(t.model)
    if spec is None:
        return [
            _error(
                "triage-model",
                f"triage.model {t.model!r} is not in models.yaml",
                f"set triage.model to one of: {', '.join(models)}",
            )
        ]
    if spec.remote and not t.allow_remote:
        results.append(
            _error("triage-model", f"model {spec.name} is remote and triage.allow_remote is false", "choose a local model")
        )
    else:
        results.append(_ok("triage-model", f"model {spec.name} ({spec.repo})"))
    if not spec.remote:
        venv_python = deciders_path / spec.venv / "bin" / "python"
        if venv_python.exists():
            results.append(_ok("triage-venv", f"environment {spec.venv}"))
        else:
            results.append(_error("triage-venv", f"environment {spec.venv} is missing", "./deciders/setup.sh"))
        snapshot = hf_hub_cache() / f"models--{spec.repo.replace('/', '--')}" / "snapshots" / spec.revision
        if _has_weights(snapshot):
            results.append(_ok("triage-weights", f"weights of {spec.name} are in the Hugging Face cache"))
        else:
            what = "has code but no weights" if snapshot.is_dir() else "is not downloaded"
            fix = f"./deciders/setup.sh --download {spec.name}"
            if t.online:
                results.append(_warn("triage-weights", f"{spec.name} {what} (triage.online: it will download on first use)", fix))
            else:
                results.append(_error("triage-weights", f"{spec.name} {what} (pinned revision {spec.revision[:8]})", fix))
    enabled = [] if isinstance(domains, ConfigError) else [d for d in domains if d.enabled]
    problems = []
    for label, overrides in [("", {})] + [(f"domain {d.id}: ", d.triage_questions) for d in enabled]:
        try:
            triage.validate_thresholds(t.thresholds, deciders.merge_questions(base, overrides))
        except ConfigError as exc:
            problems.append(f"{label}{exc}")
    if problems:
        results.append(_error("triage-thresholds", problems[0], "fix triage.thresholds in .config.yaml (or the domain's triage.questions)"))
    else:
        results.append(_ok("triage-thresholds", "thresholds match the enabled questions"))
    return results


# --- embeddings and sending -------------------------------------------------------------------------


def live_models_probe(cfg: AppConfig) -> list[str]:
    """Names of the models pulled into Ollama (`GET /api/tags`, short timeout)."""
    resp = httpx.get(cfg.embed.url.rstrip("/") + "/api/tags", timeout=3.0)
    resp.raise_for_status()
    return [str(m["name"]) for m in resp.json().get("models", [])]


def _has_model(names: list[str], model: str) -> bool:
    return any(n == model or (":" not in model and n == f"{model}:latest") for n in names)


def _embed(cfg: AppConfig, models_probe: Callable[[], list[str]] | None) -> list[Result]:
    if not cfg.embed.enabled:
        return []
    pull = f"ollama pull {cfg.embed.model}"
    if shutil.which("ollama") is None:
        return [_warn("embed-ollama", "the ollama binary is not on PATH", f"install Ollama from https://ollama.com, then `{pull}`")]
    results = [_ok("embed-ollama", "ollama binary found")]
    try:
        names = (models_probe or (lambda: live_models_probe(cfg)))()
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
        results.append(_warn("embed-model", f"Ollama is not reachable at {cfg.embed.url}", "start it with `ollama serve`"))
    else:
        if _has_model(names, cfg.embed.model):
            results.append(_ok("embed-model", f"embedding model {cfg.embed.model} is pulled"))
        else:
            results.append(_warn("embed-model", f"embedding model {cfg.embed.model} is not pulled", f"`{pull}`"))
    return results


def _send(cfg: AppConfig, send: bool) -> list[Result]:
    if not send:
        return [
            _warn(
                "send",
                "sending is off: the digest is a preview only",
                "set `notify.enabled: true` and `run.send: true` in .config.yaml (or pass --send)",
            )
        ]
    results: list[Result] = []
    if not cfg.notify.enabled:
        results.append(_error("send", "sending is requested but notify.enabled is false", "set `notify.enabled: true` in .config.yaml, or drop --send"))
    env = config.read_env()
    missing = [k for k in ("REPKA_TG_API_ID", "REPKA_TG_API_HASH") if not env.get(k, "").strip()]
    if missing:
        results.append(
            _error("telegram-credentials", f"missing in .env: {', '.join(missing)}", "add them to .env (app credentials from https://my.telegram.org)")
        )
    else:
        results.append(_ok("telegram-credentials", "REPKA_TG_API_ID and REPKA_TG_API_HASH are set"))
    session = config.session_file_path()
    if session.exists():
        results.append(_ok("telegram-session", "Telegram session found"))
    else:
        results.append(_error("telegram-session", f"no Telegram session at {session}", "uv run repka notify login"))
    if cfg.notify.chat_id.strip():
        results.append(_ok("telegram-chat", f"notify.chat_id = {cfg.notify.chat_id}"))
    else:
        results.append(_error("telegram-chat", "notify.chat_id is empty", "set `notify.chat_id` in .config.yaml ('me' = Saved Messages)"))
    return results


# --- public API -------------------------------------------------------------------------------------


def diagnose(
    cfg: AppConfig,
    *,
    send: bool,
    auth_probe: Callable[[], AuthInfo] | None = None,
    now: datetime | None = None,
    models_probe: Callable[[], list[str]] | None = None,
) -> list[Result]:
    """Every check, in a fixed order, ok ones included (what `repka doctor` prints)."""
    now = now or datetime.now(UTC)
    try:
        domains: list[Domain] | ConfigError = load_domains()
    except ConfigError as exc:
        domains = exc
    return [
        *_github(cfg, auth_probe, now),
        *_domains(domains),
        *_triage(cfg, domains),
        *_embed(cfg, models_probe),
        *_send(cfg, send),
    ]


def findings_of(results: list[Result]) -> list[Finding]:
    """The problems among `results`, errors first (stable within a level)."""
    problems = [Finding("error" if r.status == "error" else "warning", r.title, r.fix) for r in results if r.status != "ok"]
    return sorted(problems, key=lambda f: f.level != "error")


def check(
    cfg: AppConfig,
    *,
    send: bool,
    auth_probe: Callable[[], AuthInfo] | None = None,
    now: datetime | None = None,
) -> list[Finding]:
    return findings_of(diagnose(cfg, send=send, auth_probe=auth_probe, now=now))


def format_findings(findings: list[Finding]) -> str:
    """A numbered to-do list, errors first."""
    ordered = sorted(findings, key=lambda f: f.level != "error")
    return "\n".join(f"{i}. [{f.level}] {f.title}\n   fix: {f.fix}" for i, f in enumerate(ordered, 1))


def format_results(results: list[Result]) -> str:
    """One line per check: `<status> <id> <title>` and, for a problem, ` -> <fix>`."""
    width = max((len(r.id) for r in results), default=0)
    return "\n".join(
        f"{r.status:<5}  {r.id:<{width}}  {r.title}" + (f"  -> fix: {r.fix}" if r.fix else "") for r in results
    )
