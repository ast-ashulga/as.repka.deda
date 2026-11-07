"""`repka triage [--domain ID] [--limit N] [--dry-run] [--json]`."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from repka import config
from repka.config import AppConfig, ConfigError, load_config
from repka.domain.schema import Domain, get_domain, load_domains
from repka.github.client import GitHubClient
from repka.runlock import run_exclusive
from repka.store.db import connect, utcnow
from repka.store.repo import SqliteEtagCache
from repka.textutil import printable
from repka.triage import deciders, triage
from repka.triage.deciders import QuestionSet, WorkerError

EPILOG = """\
Runs the decider model on the repos of a domain that have no verdict yet (or whose description, topics or
language changed, or whose questions/model changed) and stores the verdicts. A repo is `accepted` when every
threshold in `triage.thresholds` passes. Verdicts are idempotent: a second run decides nothing new.
A crashed or timed-out worker, a failed item or a rate-limited README fetch leaves the repo pending, never
accepted or rejected. --dry-run loads no model and writes nothing.
"""


def _positive(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return n


def _select_domains(domain_id: str | None) -> list[Domain]:
    if domain_id:
        return [get_domain(domain_id)]  # explicit: runs even if the domain is not enabled
    return [d for d in load_domains() if d.enabled]


def question_sets(cfg: AppConfig, domains: list[Domain]) -> dict[str, QuestionSet]:
    """Per-domain question sets; thresholds are checked against each before any README or model is touched."""
    base = deciders.load_questions(config.project_root() / cfg.triage.deciders_dir / "questions.yaml")
    sets: dict[str, QuestionSet] = {}
    for d in domains:
        q = deciders.merge_questions(base, d.triage_questions)
        try:
            triage.validate_thresholds(cfg.triage.thresholds, q)
        except ConfigError as exc:
            raise ConfigError(f"domain {d.id}: {exc}") from exc
        sets[d.id] = q
    return sets


def _cell(answer: dict) -> str:
    if answer["type"] == "noul":
        return f"{answer['p']:.2f}"
    return str(answer["pred"])


def _table(q: QuestionSet, decisions: list[tuple[str, dict[str, dict], bool]]) -> list[str]:
    header = ["repo"] + [f"{qid} p" if v["type"] == "noul" else qid for qid, v in q.questions.items()] + ["accepted"]
    body = [
        [printable(name)] + [_cell(answers[qid]) if qid in answers else "-" for qid in q.questions]
        + ["yes" if accepted else "no"]
        for name, answers, accepted in decisions
    ]
    widths = [max(len(r[i]) for r in [header, *body]) for i in range(len(header))]
    return ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in [header, *body]]


def _dry_run(conn: sqlite3.Connection, cfg: AppConfig, domains: list[Domain], sets: dict[str, QuestionSet], as_json: bool, limit: int | None) -> int:
    now = utcnow()
    report = []
    for d in domains:
        q = sets[d.id]
        pending = triage.pending_repos(conn, d.id, cfg.triage.model, deciders.questions_hash(q))
        if limit is not None:
            pending = pending[:limit]
        first = None
        if pending:
            excerpt = triage.fresh_excerpt(pending[0], now)
            first = triage.build_item(
                pending[0], triage.README_MISSING if excerpt is None else excerpt, d, cfg.triage.readme_chars, q.state_template
            ).state
        report.append({"domain": d.id, "pending": len(pending), "first_state": first})
    if as_json:
        print(json.dumps({"dry_run": True, "model": cfg.triage.model, "domains": report}, ensure_ascii=False, indent=2))
        return 0
    for r in report:
        print(f"{r['domain']}: {r['pending']} repo(s) pending triage with {cfg.triage.model}")
        if r["first_state"] is not None:
            print("first input:\n" + "\n".join("  " + line for line in r["first_state"].splitlines()))
    return 0


def _cmd_triage(args: argparse.Namespace) -> int:
    return run_exclusive(lambda: _triage(args), enabled=not args.dry_run)


def _triage(args: argparse.Namespace) -> int:
    cfg = load_config()
    domains = _select_domains(args.domain)
    if not domains:
        print("no enabled domains: nothing to triage (see: repka domain list)", file=sys.stderr)
        return 0
    sets = question_sets(cfg, domains)
    if not cfg.triage.enabled:
        print(
            "note: triage.enabled is false in .config.yaml; that flag only governs `repka run`. "
            "Triage is running because `repka triage` was invoked explicitly.",
            file=sys.stderr,
        )
    path = config.db_path()
    if not path.exists():
        raise ConfigError(f"no database at {path}; run `repka db migrate` first")
    if args.dry_run:
        conn = connect(readonly=True)
        try:
            return _dry_run(conn, cfg, domains, sets, args.json, args.limit)
        finally:
            conn.close()

    models = deciders.load_models(config.project_root() / cfg.triage.deciders_dir / "models.yaml")
    backend = deciders.make_backend(cfg.triage, models)  # ConfigError for an unknown or disabled remote model
    conn = connect()
    client: GitHubClient | None = None

    def readme_fn(full_name: str) -> str:
        nonlocal client
        if client is None:  # only needed (and the token only checked) when a README is not stored yet
            client = GitHubClient(
                config.github_token(), cfg.github, token_source=config.github_token_source(), cache=SqliteEtagCache(conn)
            )
        return client.readme(full_name)

    now = utcnow()
    results = []
    failure: str | None = None
    try:
        for d in domains:
            try:
                stats = triage.triage_domain(
                    conn, d, backend, sets[d.id], cfg.triage.model, cfg.triage.thresholds,
                    readme_fn, cfg.triage.readme_chars, now, args.limit,
                )  # fmt: skip
            except WorkerError as exc:  # nothing was written for the failed chunk; the repos stay pending
                failure = f"{exc}; affected repos stay pending and are retried by the next run"
                break
            results.append((d, stats))
    finally:
        if client is not None:
            client.close()
        conn.close()
    _report(results, sets, args.json)
    if any(s.rate_limited for _, s in results):
        print("warning: GitHub rate limit hit while fetching READMEs; repos needing one stay pending", file=sys.stderr)
    if failure:
        print(f"error: {failure}", file=sys.stderr)
        return 1
    return 0


def _report(results: list[tuple[Domain, triage.TriageStats]], sets: dict[str, QuestionSet], as_json: bool) -> None:
    if as_json:
        out = [
            {
                "domain": d.id,
                "pending": s.pending,
                "decided": s.decided,
                "accepted": s.accepted,
                "errors": s.errors,
                "rate_limited": s.rate_limited,
                "results": [
                    {"repo": name, "answers": answers, "accepted": accepted} for name, answers, accepted in s.decisions
                ],
            }
            for d, s in results
        ]
        print(json.dumps({"dry_run": False, "domains": out}, ensure_ascii=False, indent=2))
        return
    for d, s in results:
        print(f"{d.id}: pending {s.pending}, decided {s.decided}, accepted {s.accepted}, errors {s.errors}")
        if s.decisions:
            print("\n".join(_table(sets[d.id], s.decisions)))


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "triage",
        help="decide pending repos with the local decider model and store verdicts",
        description="Decide which repos of a domain are relevant and substantial, and store the verdicts.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--domain", metavar="ID", help="only this domain (default: every enabled domain)")
    p.add_argument("--limit", type=_positive, metavar="N", help="decide at most N pending repos per domain")
    p.add_argument("--dry-run", action="store_true", help="print the pending count and the first model input; loads no model")
    p.add_argument("--json", action="store_true", help="print JSON instead of a summary and table")
    p.set_defaults(func=_cmd_triage)
