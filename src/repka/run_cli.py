"""`repka run [--send] [--domain ID] [--json]`: one full pass (what launchd calls)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime

from repka import pipeline, preflight
from repka.cliutil import stderr_progress
from repka.collect_cli import domain_line
from repka.config import ConfigError, domains_dir, load_config, state_dir
from repka.digest.run import failure_note
from repka.domain.schema import Domain, get_domain, load_domains
from repka.pipeline import PassReport
from repka.progress import RunProgress
from repka.progress.active import activate, stderr_line
from repka.progress.eta import history_medians
from repka.progress.render import MODES, JsonlRenderer, make_renderer
from repka.progress.status import StatusFileSink
from repka.runlock import LockBusy, RunLock
from repka.store import repo
from repka.store.db import connect, migrate

log = logging.getLogger("repka.run")

EPILOG = """\
Order: collect (domains that are due) -> triage (only if triage.enabled) -> embed (only if embed.enabled) ->
digest (always built; delivered only with --send or run.send, and notify.enabled). A failing stage never stops
the digest: a GitHub rate limit or a crashed worker makes the run `partial`, and the next run continues.
Progress goes to stderr (`--progress`/`run.progress`: plain = one line per event, live = redrawn status line with
ETA on a terminal, lines = timestamped lines for logs, jsonl = one JSON event per line for programs); the summary
(or --json) goes to stdout. Whatever the mode, `.state/progress.json` and `repka status` show a running pass. Exit 0 for ok/partial, 1 for failed, 2 for a
configuration error. Only one run at a time: a second one exits 0 immediately. Every run is recorded in `runs`
(see `repka db stats`).
"""


def _make_progress(conn, mode: str, run_id: int):
    """The run's `RunProgress` (state machine) and the renderer that owns stderr (None for `plain`)."""
    rows = conn.execute(
        "SELECT stats_json FROM runs WHERE kind = 'run' AND status IN ('ok', 'partial') ORDER BY id DESC LIMIT 20"
    ).fetchall()
    renderer = make_renderer(mode, sys.stderr, is_tty=sys.stderr.isatty(), clock=time.monotonic, wall=time.time)
    status_file = StatusFileSink(state_dir() / "progress.json", os.getpid(), run_id, mode, heartbeat_thread=True)
    sinks = [status_file] if renderer is None else [renderer, status_file]
    return RunProgress(history=history_medians(r["stats_json"] for r in rows), sinks=sinks), renderer


# collect narrates every query and enrichment batch; the structured events already carry that (and are throttled)
_BATCH_NOTE = re.compile(r"^\[\d+/\d+\]\s+(query \d+/\d+ done|enriched \d+/\d+)")


def _without_batch_notes(note):
    """Wrap a note callback so per-query and per-batch collect lines (covered by progress events) are dropped."""

    def filtered(message: str) -> None:
        if not _BATCH_NOTE.match(message):
            note(message)

    return filtered


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
        "failed": f"digest   {where}: send failed, {failure_note(d)}",
    }[d.status]


def _releases_line(report: PassReport) -> str:
    r = report.releases
    if r is None:
        return "releases not built (stage failed)"
    where = f"slot {r.slot} {r.local_date}"
    return {
        "not_due": "releases no slot due",
        "empty": f"releases {where}: nothing to send" if report.send else f"releases {where}: nothing pending",
        "preview": f"releases {where}: preview of {r.item_count} item(s) in {len(r.parts)} message(s), not sent",
        "sent": f"releases {where}: sent {r.item_count} item(s) in {len(r.parts)} message(s)",
        "failed": f"releases {where}: send failed, {failure_note(r)}",
    }[r.status]


def _text(run_id: int, report: PassReport, hints: list[str], seconds: float, releases_on: bool = False) -> str:
    lines = [f"run #{run_id}: {report.status} in {seconds:.0f}s"]
    if report.collect is not None:
        lines += [f"collect  {domain_line(r)}" for r in report.collect.domains] or ["collect  nothing due"]
    lines += [
        f"triage   {d}: pending {s.pending}, decided {s.decided}, accepted {s.accepted}, errors {s.errors}"
        for d, s in report.triage.items()
    ]
    lines += [f"embed    {d}: pending {s.pending}, embedded {s.embedded}" for d, s in report.embed.items()]
    lines.append(_digest_line(report))
    if releases_on:
        lines.append(_releases_line(report))
    lines += ["errors:", *(f"  - {e}" for e in report.errors)] if report.errors else []
    lines += ["hints:", *(f"  - {h}" for h in hints)] if hints else []
    d = report.digest
    if d is not None and d.status == "preview":
        for i, part in enumerate(d.parts, 1):
            lines += [f"--- digest message {i}/{len(d.parts)} (preview) ---", part]
    r = report.releases
    if r is not None and r.status == "preview":
        for i, part in enumerate(r.parts, 1):
            lines += [f"--- releases message {i}/{len(r.parts)} (preview) ---", part]
    return "\n".join(lines)


def _run(args: argparse.Namespace) -> int:
    cfg = load_config()
    mode = args.progress or cfg.run.progress
    if mode != "jsonl":
        return _run_checked(args, cfg)
    # jsonl promises that every stderr line is JSON, also the ones before the run proper starts
    early = JsonlRenderer(sys.stderr, time.monotonic, time.time)
    with activate(early):
        try:
            return _run_checked(args, cfg)
        except ConfigError as exc:
            stderr_line(f"error: {exc}", "ERROR")
            return 2


def _run_checked(args: argparse.Namespace, cfg) -> int:
    if args.send and args.domain:
        raise ConfigError(
            "--send cannot be combined with --domain: a digest consumes the slot for ALL domains, "
            "so it must cover them all. Drop --domain to send, or drop --send to preview this domain"
        )
    send = (cfg.run.send and not args.domain) or args.send  # a single-domain run is preview-only
    if send and not cfg.notify.enabled:
        raise ConfigError(
            "sending is requested (run.send or --send) but notify.enabled is false: "
            "set `notify.enabled: true` in .config.yaml, or drop --send"
        )
    domains = _select(args.domain)
    findings = preflight.check(cfg, send=send)  # before the lock and before any search or database write
    if findings:
        stderr_line(preflight.format_findings(findings))
    if any(f.level == "error" for f in findings):
        stderr_line("error: repka run did not start: fix the errors above (all checks: uv run repka doctor)", "ERROR")
        return 2
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        with RunLock(state_dir() / "run.lock"):
            return _run_locked(args, cfg, domains, now, send)
    except LockBusy as exc:
        stderr_line(f"another repka run is in progress ({exc}); nothing done")
        return 0


def _run_locked(args: argparse.Namespace, cfg, domains: list[Domain], now: datetime, send: bool) -> int:
    conn = connect()
    boundaries = None
    progress = None
    try:
        migrate(conn)
        run_id = repo.start_run(conn, "run")
        conn.commit()
        started = time.monotonic()
        mode = args.progress or cfg.run.progress
        progress, renderer = _make_progress(conn, mode, run_id)
        legacy = stderr_progress if renderer is None else None  # `plain` keeps today's lines; the others use events
        with activate(renderer):
            stderr_line(f"run #{run_id}: {len(domains)} domain(s), send={'yes' if send else 'no'}")
            try:
                boundaries = pipeline.build_boundaries(
                    conn, cfg, domains, force=bool(args.domain), progress=legacy or _without_batch_notes(progress.note), reporter=progress
                )
                report = pipeline.run_pass(domains, cfg, boundaries, now, send, progress=legacy, reporter=progress)
            except BaseException as exc:  # run_pass catches stage errors; this is the net under it (also Ctrl-C):
                log.exception("run #%s crashed", run_id)  # the run is still recorded, never left without a status
                repo.finish_run(conn, run_id, "failed", {}, f"{type(exc).__name__}: {exc}")
                conn.commit()
                progress.finish("failed")  # ends the live line and finalises the status file
                if not isinstance(exc, Exception):
                    raise
                stderr_line(f"error: run crashed: {exc} (see logs/repka.log)", "ERROR")
                return 1
            errors = "; ".join(report.errors) or None
            stats = {**pipeline.report_dict(report, full=False), "timings": progress.timings()}
            repo.finish_run(conn, run_id, report.status, stats, errors)
            conn.commit()
            seconds = time.monotonic() - started
            progress.finish(report.status)
            hints = pipeline.explain(report, cfg)
            if args.json:
                payload = {"run_id": run_id, "duration_s": round(seconds, 1), **pipeline.report_dict(report, full=True), "hints": hints}
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                stderr_line(f"run #{run_id}: {report.status} in {seconds:.0f}s")
            else:
                print(_text(run_id, report, hints, seconds, cfg.digest.releases_message))
            return 1 if report.status == "failed" else 0
    finally:
        if progress is not None:
            progress.finish("failed")  # no-op once finished; covers an exception outside the net above
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
    run.add_argument("--domain", metavar="ID", help="only this domain, collected even if it is not due (preview only: no --send)")
    run.add_argument(
        "--progress",
        choices=MODES,
        metavar="MODE",
        help="stderr progress: plain (default, one line per event), live (status line with ETA on a terminal), "
        "lines (timestamped, for logs), jsonl (JSON events). Default: run.progress in .config.yaml",
    )
    run.add_argument("--json", action="store_true", help="print the report as JSON on stdout (summary line on stderr)")
    run.set_defaults(func=_run)
