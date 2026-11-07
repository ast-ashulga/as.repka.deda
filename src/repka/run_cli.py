"""`repka run [--send] [--domain ID] [--json]`: one full pass (what launchd calls)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime

from repka import pipeline
from repka.collect_cli import _line
from repka.config import ConfigError, domains_dir, load_config, state_dir
from repka.domain.schema import Domain, get_domain, load_domains
from repka.pipeline import PassReport
from repka.runlock import LockBusy, RunLock
from repka.store import repo
from repka.store.db import connect, migrate

log = logging.getLogger("repka.run")

EPILOG = """\
Order: collect (domains that are due) -> triage (only if triage.enabled) -> embed (only if embed.enabled) ->
digest (always built; delivered only with --send or run.send, and notify.enabled). A failing stage never stops
the digest: a GitHub rate limit or a crashed worker makes the run `partial`, and the next run continues.
Progress goes to stderr; the summary (or --json) goes to stdout. Exit 0 for ok/partial, 1 for failed, 2 for a
configuration error. Only one run at a time: a second one exits 0 immediately. Every run is recorded in `runs`
(see `repka db stats`).
"""


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _select(domain_id: str | None) -> list[Domain]:
    if domain_id:
        d = get_domain(domain_id)
        if not d.enabled:
            raise ConfigError(
                f"domain {d.id!r} is disabled: set `enabled: true` in {domains_dir() / (d.id + '.yaml')} to run it"
            )
        return [d]
    return [d for d in load_domains() if d.enabled]


def _digest_line(report: PassReport) -> str:
    d = report.digest
    if d is None:
        return "digest   not built (stage failed)"
    where = f"slot {d.slot} {d.local_date}"
    return {
        "not_due": "digest   no slot due",
        "empty": f"digest   {where}: nothing to send" if report.send else f"digest   {where}: nothing pending",
        "preview": f"digest   {where}: preview of {d.item_count} item(s) in {len(d.parts)} message(s), not sent",
        "sent": f"digest   {where}: sent {d.item_count} item(s) in {len(d.parts)} message(s)",
        "failed": f"digest   {where}: send failed, slot stays open for the next run",
    }[d.status]


def _text(run_id: int, report: PassReport, hints: list[str], seconds: float) -> str:
    lines = [f"run #{run_id}: {report.status} in {seconds:.0f}s"]
    if report.collect is not None:
        lines += [f"collect  {_line(r)}" for r in report.collect.domains] or ["collect  nothing due"]
    lines += [
        f"triage   {d}: pending {s.pending}, decided {s.decided}, accepted {s.accepted}, errors {s.errors}"
        for d, s in report.triage.items()
    ]
    lines += [f"embed    {d}: pending {s.pending}, embedded {s.embedded}" for d, s in report.embed.items()]
    lines.append(_digest_line(report))
    lines += ["errors:", *(f"  - {e}" for e in report.errors)] if report.errors else []
    lines += ["hints:", *(f"  - {h}" for h in hints)] if hints else []
    d = report.digest
    if d is not None and d.status == "preview":
        for i, part in enumerate(d.parts, 1):
            lines += [f"--- digest message {i}/{len(d.parts)} (preview) ---", part]
    return "\n".join(lines)


def _run(args: argparse.Namespace) -> int:
    cfg = load_config()
    send = cfg.run.send or args.send
    if send and not cfg.notify.enabled:
        raise ConfigError(
            "sending is requested (run.send or --send) but notify.enabled is false: "
            "set `notify.enabled: true` in .config.yaml, or drop --send"
        )
    domains = _select(args.domain)
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        with RunLock(state_dir() / "run.lock"):
            return _run_locked(args, cfg, domains, now, send)
    except LockBusy as exc:
        print(f"another repka run is in progress ({exc}); nothing done", file=sys.stderr)
        return 0


def _run_locked(args: argparse.Namespace, cfg, domains: list[Domain], now: datetime, send: bool) -> int:
    conn = connect()
    boundaries = None
    try:
        migrate(conn)
        # T24 adds the preflight (token, Ollama, Telegram session) here.
        run_id = repo.start_run(conn, "run")
        conn.commit()
        started = time.monotonic()
        _progress(f"run #{run_id}: {len(domains)} domain(s), send={'yes' if send else 'no'}")
        try:
            boundaries = pipeline.build_boundaries(conn, cfg, domains, force=bool(args.domain), progress=_progress)
            report = pipeline.run_pass(domains, cfg, boundaries, now, send, progress=_progress)
        except BaseException as exc:  # run_pass catches stage errors; this is the net under it (also Ctrl-C):
            log.exception("run #%s crashed", run_id)  # the run is still recorded, never left without a status
            repo.finish_run(conn, run_id, "failed", {}, f"{type(exc).__name__}: {exc}")
            conn.commit()
            if not isinstance(exc, Exception):
                raise
            print(f"error: run crashed: {exc} (see logs/repka.log)", file=sys.stderr)
            return 1
        errors = "; ".join(report.errors) or None
        repo.finish_run(conn, run_id, report.status, pipeline.report_dict(report, full=False), errors)
        conn.commit()
        seconds = time.monotonic() - started
        hints = pipeline.explain(report, cfg)
        if args.json:
            payload = {"run_id": run_id, "duration_s": round(seconds, 1), **pipeline.report_dict(report, full=True), "hints": hints}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            print(f"run #{run_id}: {report.status} in {seconds:.0f}s", file=sys.stderr)
        else:
            print(_text(run_id, report, hints, seconds))
        return 1 if report.status == "failed" else 0
    finally:
        if boundaries is not None:
            boundaries.close()
        conn.close()


def register(sub: argparse._SubParsersAction) -> None:
    run = sub.add_parser(
        "run",
        help="one full pass: collect, triage, embed, digest (what launchd calls)",
        description="Run the whole pipeline once for the enabled domains (or only --domain ID).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument("--send", action="store_true", help="deliver the digest to Telegram (same as run.send: true)")
    run.add_argument("--domain", metavar="ID", help="only this domain, collected even if it is not due")
    run.add_argument("--json", action="store_true", help="print the report as JSON on stdout (summary line on stderr)")
    run.set_defaults(func=_run)
