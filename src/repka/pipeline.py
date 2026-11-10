"""One `repka run` pass: collect -> triage -> embed -> digest (-> releases message), with every boundary injected (architecture § 5).

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
from repka.digest.releases import run_releases
from repka.digest.run import DigestOutcome, run_digest
from repka.domain.schema import Domain
from repka.embed import cli as embed_cli
from repka.embed.embed import EmbedStats, embed_domain
from repka.embed.ollama import EmbedderUnavailable
from repka.github.client import AuthError, GitHubClient, RateLimited
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
    releases: Callable[[datetime, bool], DigestOutcome] | None = None  # the daily releases message


@dataclass
class PassReport:
    status: Literal["ok", "partial", "failed"]
    collect: CollectReport | None = None
    triage: dict[str, TriageStats] = field(default_factory=dict)
    embed: dict[str, EmbedStats] = field(default_factory=dict)
    digest: DigestOutcome | None = None
    releases: DigestOutcome | None = None
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

    def fail(stage: str, exc: Exception) -> bool:
        """Record a stage error; True when it is an expected one (known, recoverable)."""
        nonlocal problems
        problems += 1
        known = isinstance(exc, EXPECTED)
        if known:
            log.warning("%s stage stopped: %s", stage, exc)
        else:
            log.exception("%s stage failed", stage)
        report.errors.append(_err(stage, exc))
        return known

    def per_domain(stage: str, work: Callable[[Domain], None]) -> None:
        """Run `work` for each enabled domain. One domain's failure never skips the others, except a backend-level
        one (crashed worker, Ollama down, rejected GitHub token) that the next domain would hit too: that ends the stage."""
        nonlocal attempted, unexpected
        attempted += 1
        raised_unexpected = succeeded = 0
        for d in enabled:
            try:
                work(d)
            except Exception as exc:
                known = fail(f"{stage} {d.id}", exc)
                raised_unexpected += not known
                if isinstance(exc, (WorkerError, EmbedderUnavailable, AuthError)):
                    break
            else:
                succeeded += 1
        unexpected += bool(raised_unexpected) and not succeeded

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
            unexpected += not fail("collect", exc)

    def triage_one(d: Domain) -> None:
        nonlocal problems
        say(f"triage: {d.id}")
        stats = report.triage[d.id] = b.triage(d, now)
        say(f"triage: {d.id}: decided {stats.decided}, accepted {stats.accepted}, pending {stats.pending}")
        if stats.rate_limited:
            problems += 1
            report.errors.append(f"triage {d.id}: GitHub rate limit hit while fetching READMEs; repos stay pending")
        if stats.errors:
            problems += 1
            report.errors.append(f"triage {d.id}: {stats.errors} repo(s) stay pending after an error")

    def embed_one(d: Domain) -> None:
        say(f"embed: {d.id}")
        stats = report.embed[d.id] = b.embed(d, now)
        say(f"embed: {d.id}: embedded {stats.embedded} of {stats.pending}")

    if enabled and cfg.triage.enabled:
        per_domain("triage", triage_one)
    if enabled and cfg.embed.enabled:
        per_domain("embed", embed_one)

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
        unexpected += not fail("digest", exc)

    if cfg.digest.releases_message and b.releases is not None:  # its own message, after the digest; same failure rules
        attempted += 1
        say("releases: " + ("building and sending" if send else "building a preview (not sent)"))
        try:
            report.releases = b.releases(now, send)
            if report.releases.status == "failed":
                problems += 1
                report.errors.append(f"releases: send failed: {printable(report.releases.error or 'unknown error')}")
            elif report.releases.error:
                problems += 1
                report.errors.append(f"releases: {printable(report.releases.error)}")
        except Exception as exc:
            unexpected += not fail("releases", exc)

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
    r = report.releases
    if r is not None and r.status == "not_due":
        hints.append("no releases slot is due now (digest.releases_slot), so no releases message was built")
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
        return collect_domains(client(), conn, due, cfg, now, progress=say, all_domains=domains)

    def triage_stage(d: Domain, now: datetime) -> TriageStats:
        if "backend" not in held:
            models = deciders.load_models(config.project_root() / cfg.triage.deciders_dir / "models.yaml")
            held["backend"] = deciders.make_backend(cfg.triage, models)  # ConfigError for a bad/remote model
        if d.id not in questions:
            questions.update(triage_cli.question_sets(cfg, [d]))  # also validates the thresholds
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

    def releases_stage(now: datetime, send: bool) -> DigestOutcome:
        sender = digest_cli.make_sender(cfg) if send else None
        return run_releases(conn, [d for d in domains if d.enabled], cfg, sender, now, send)

    def close() -> None:
        for key in ("client", "embedder"):
            if key in held:
                held[key].close()

    return Boundaries(collect, triage_stage, embed_stage, digest_stage, close, releases_stage)


def report_dict(report: PassReport, *, full: bool) -> dict[str, Any]:
    """JSON-ready view of a pass. `full=False` (the `runs.stats_json` row) leaves out digest text and decisions."""

    def outcome(o: DigestOutcome | None) -> dict[str, Any] | None:
        if o is None:
            return None
        d = dataclasses.asdict(o)
        if not full:
            d.pop("parts")
        return d

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
        "digest": outcome(report.digest),
        "releases": outcome(report.releases),
        "errors": report.errors,
    }
