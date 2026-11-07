"""One `repka run` pass: collect -> triage -> embed -> digest, with every boundary injected (architecture § 5).

`run_pass` is pure orchestration. A stage that raises is logged and recorded in `errors`, and the pass goes on:
the digest stage always runs, so a GitHub rate limit (RF1) or a crashed decider worker (RF7) never silences it.
Status: `ok`; `partial` when anything went wrong or was left unfinished; `failed` only when every stage that
ran raised an unexpected exception.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from repka import collect_cli, config
from repka.collect import CollectReport, collect_domains, due_domains, iso, next_due
from repka.config import AppConfig
from repka.digest import cli as digest_cli
from repka.digest.run import DigestOutcome, run_digest
from repka.domain.schema import Domain
from repka.embed import cli as embed_cli
from repka.embed.embed import EmbedStats, embed_domain
from repka.embed.ollama import EmbedderUnavailable
from repka.github.client import GitHubClient, RateLimited
from repka.store import repo
from repka.textutil import printable
from repka.triage import cli as triage_cli
from repka.triage import deciders, triage
from repka.triage.deciders import WorkerError
from repka.triage.triage import TriageStats

log = logging.getLogger("repka.pipeline")

Progress = Callable[[str], None]
EXPECTED = (RateLimited, WorkerError, EmbedderUnavailable)  # known, recoverable: the pass is `partial`


@dataclass
class Boundaries:
    collect: Callable[[list[Domain], datetime], CollectReport]
    triage: Callable[[Domain, datetime], TriageStats]
    embed: Callable[[Domain, datetime], EmbedStats]
    digest: Callable[[datetime, bool], DigestOutcome]
    close: Callable[[], None] = lambda: None  # releases whatever the real boundaries opened lazily


@dataclass
class PassReport:
    status: Literal["ok", "partial", "failed"]
    collect: CollectReport | None = None
    triage: dict[str, TriageStats] = field(default_factory=dict)
    embed: dict[str, EmbedStats] = field(default_factory=dict)
    digest: DigestOutcome | None = None
    errors: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)  # enabled domains of this pass (for `explain`)
    send: bool = False


def _err(stage: str, exc: Exception) -> str:
    return f"{stage}: {type(exc).__name__}: {printable(str(exc))}"


def run_pass(
    domains: list[Domain],
    cfg: AppConfig,
    b: Boundaries,
    now: datetime,
    send: bool,
    progress: Progress | None = None,
) -> PassReport:
    """collect (due domains) -> triage (if triage.enabled) -> embed (if embed.enabled) -> digest (always)."""
    say = progress or (lambda message: None)
    enabled = [d for d in domains if d.enabled]
    report = PassReport("ok", domains=[d.id for d in enabled], send=send)
    attempted = unexpected = 0
    problems = 0  # anything that makes the pass less than complete

    def fail(stage: str, exc: Exception) -> None:
        nonlocal unexpected, problems
        problems += 1
        known = isinstance(exc, EXPECTED)
        unexpected += not known
        if known:
            log.warning("%s stage stopped: %s", stage, exc)
        else:
            log.exception("%s stage failed", stage)
        report.errors.append(_err(stage, exc))

    if enabled:
        attempted += 1
        say(f"collect: {len(enabled)} enabled domain(s)")
        try:
            report.collect = b.collect(enabled, now)
            for r in report.collect.domains:
                if r.status != "ok":
                    problems += 1
                    detail = f": {printable(r.error)}" if r.error else ""
                    report.errors.append(f"collect {r.domain_id}: {r.status}{detail}")
        except Exception as exc:
            fail("collect", exc)

    if enabled and cfg.triage.enabled:
        attempted += 1
        try:
            for d in enabled:
                say(f"triage: {d.id}")
                stats = report.triage[d.id] = b.triage(d, now)
                say(f"triage: {d.id}: decided {stats.decided}, accepted {stats.accepted}, pending {stats.pending}")
                if stats.rate_limited:
                    problems += 1
                    report.errors.append(f"triage {d.id}: GitHub rate limit hit while fetching READMEs; repos stay pending")
                if stats.errors:
                    problems += 1
                    report.errors.append(f"triage {d.id}: {stats.errors} repo(s) stay pending after an error")
        except Exception as exc:
            fail("triage", exc)

    if enabled and cfg.embed.enabled:
        attempted += 1
        try:
            for d in enabled:
                say(f"embed: {d.id}")
                stats = report.embed[d.id] = b.embed(d, now)
                say(f"embed: {d.id}: embedded {stats.embedded} of {stats.pending}")
        except Exception as exc:
            fail("embed", exc)

    attempted += 1
    say("digest: " + ("building and sending" if send else "building a preview (not sent)"))
    try:
        report.digest = b.digest(now, send)
        if report.digest.status == "failed":
            problems += 1
            report.errors.append(f"digest: send failed: {printable(report.digest.error or 'unknown error')}")
        elif report.digest.error:  # a later message of the digest failed after one was delivered
            problems += 1
            report.errors.append(f"digest: {printable(report.digest.error)}")
    except Exception as exc:
        fail("digest", exc)

    if unexpected == attempted:
        report.status = "failed"
    elif problems:
        report.status = "partial"
    return report


def explain(report: PassReport, cfg: AppConfig) -> list[str]:
    """Hints that tell a human or an agent why a pass did less than they may expect."""
    hints = []
    if not report.domains:
        hints.append("no enabled domains: nothing was collected (see `repka domain list`; set `enabled: true` in a domain file)")
    if not cfg.triage.enabled:
        hints.append(
            "triage is disabled (triage.enabled: false), so nothing is accepted and digests stay empty"
        )
    if not cfg.embed.enabled:
        hints.append("embeddings are disabled (embed.enabled: false), so `repka similar` has no fresh data")
    c = report.collect
    if c is not None and c.rate_limited:
        when = ""
        if c.retry_at:
            when = " after " + datetime.fromtimestamp(c.retry_at).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        hints.append(f"GitHub rate limit reached: collected data is kept; the next run continues (retry{when})")
    d = report.digest
    if d is not None and d.status == "not_due":
        hints.append("no digest slot is due now (digest.slots), so no digest was built")
    if not report.send:
        if not cfg.notify.enabled:
            hints.append("digest preview only: set notify.enabled and run.send (or pass --send) to deliver digests")
        else:
            hints.append("digest preview only: set run.send: true (or pass --send) to deliver digests")
    return hints


# ---- real wiring ------------------------------------------------------------------------------


def build_boundaries(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    domains: list[Domain],
    *,
    force: bool = False,
    progress: Progress | None = None,
) -> Boundaries:
    """The real boundaries. Everything expensive (GitHub client, decider backend, embedder, Telegram sender)
    is built on first use, so a stage that does nothing costs nothing.

    `domains` is the pass's domain list (the digest covers exactly these); `force=True` (`--domain`) collects
    them even when they are not due.
    """
    say = progress or (lambda message: None)
    held: dict[str, Any] = {}
    questions: dict[str, deciders.QuestionSet] = {}

    def client() -> GitHubClient:
        if "client" not in held:
            held["client"] = collect_cli.make_client(cfg, conn)  # ConfigError (no token) is a stage error
        return held["client"]

    def collect(ds: list[Domain], now: datetime) -> CollectReport:
        states = {d.id: repo.get_domain_state(conn, d.id) for d in ds}
        due = ds if force else due_domains(ds, states, now)
        for d in ds:
            if d not in due:
                at = next_due(d, states[d.id])
                say(f"collect: {d.id} not due" + (f" until {iso(at)}" if at else ""))
        if not due:
            return CollectReport()
        return collect_domains(client(), conn, due, cfg, now, progress=say)

    def triage_stage(d: Domain, now: datetime) -> TriageStats:
        if "backend" not in held:
            models = deciders.load_models(config.project_root() / cfg.triage.deciders_dir / "models.yaml")
            held["backend"] = deciders.make_backend(cfg.triage, models)  # ConfigError for a bad/remote model
        if d.id not in questions:
            questions.update(triage_cli._question_sets(cfg, [d]))  # also validates the thresholds
        return triage.triage_domain(
            conn, d, held["backend"], questions[d.id], cfg.triage.model, cfg.triage.thresholds,
            lambda full_name: client().readme(full_name), cfg.triage.readme_chars, iso(now),
        )  # fmt: skip

    def embed_stage(d: Domain, now: datetime) -> EmbedStats:
        if "embedder" not in held:
            held["embedder"] = embed_cli.make_embedder(cfg)
        return embed_domain(conn, d.id, held["embedder"], cfg.embed.batch, iso(now))

    def digest_stage(now: datetime, send: bool) -> DigestOutcome:
        sender = digest_cli.make_sender(cfg) if send else None
        return run_digest(conn, [d for d in domains if d.enabled], cfg, sender, now, send)

    def close() -> None:
        for key in ("client", "embedder"):
            if key in held:
                held[key].close()

    return Boundaries(collect, triage_stage, embed_stage, digest_stage, close)


def report_dict(report: PassReport, *, full: bool) -> dict[str, Any]:
    """JSON-ready view of a pass. `full=False` (the `runs.stats_json` row) leaves out digest text and decisions."""
    digest = None
    if report.digest is not None:
        digest = dataclasses.asdict(report.digest)
        if not full:
            digest.pop("parts")
    return {
        "status": report.status,
        "send": report.send,
        "domains": report.domains,
        "collect": None
        if report.collect is None
        else {
            "rate_limited": report.collect.rate_limited,
            "domains": [
                {**dataclasses.asdict(r), "error": printable(r.error) if r.error else None}
                for r in report.collect.domains
            ],
        },
        "triage": {
            k: {f: v for f, v in dataclasses.asdict(s).items() if f != "decisions"} for k, s in report.triage.items()
        },
        "embed": {k: dataclasses.asdict(s) for k, s in report.embed.items()},
        "digest": digest,
        "errors": report.errors,
    }
