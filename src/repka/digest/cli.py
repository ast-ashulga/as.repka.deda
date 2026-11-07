"""`repka digest [--slot HH:MM] [--send] [--dry-run]`."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from repka import config
from repka.config import AppConfig, ConfigError, load_config
from repka.digest.build import build_digest
from repka.digest.render import render_digest, split_messages
from repka.digest.run import DigestOutcome, run_digest
from repka.digest.slots import parse_slot
from repka.domain.schema import load_domains
from repka.notify.telegram_user import Sender, TelegramUserSender
from repka.store.db import connect


def make_sender(cfg: AppConfig) -> Sender:
    """The real Telegram sender; tests replace this function with a fake."""
    config.telegram_credentials()  # ConfigError (exit 2) with a fix hint, not a failed send
    config.require_session()
    return TelegramUserSender(cfg.notify.chat_id, cfg.notify.topic_id)


def _print_parts(header: str, parts: list[str]) -> None:
    print(header)
    for i, part in enumerate(parts, 1):
        print(f"--- message {i}/{len(parts)} ---\n{part}")


def _latest_passed_slot(slots: list[str], now_local: datetime) -> str:
    passed = [s for s in sorted(set(slots)) if datetime.combine(now_local.date(), parse_slot(s)) <= now_local.replace(tzinfo=None)]
    return passed[-1] if passed else sorted(set(slots))[0]


def _preview_anyway(conn, domains, cfg: AppConfig, now: datetime) -> int:
    """No slot is due: still show what would be pending, so a human or an agent can inspect it."""
    local = now.astimezone()
    slot = _latest_passed_slot(cfg.digest.slots, local)
    day = local.date().isoformat()
    digest = build_digest(conn, domains, now, cfg.digest, slot, day)
    parts = split_messages(render_digest(digest))
    print("note: no digest slot is due now; showing the items pending right now", file=sys.stderr)
    if not parts:
        print(f"[preview] slot {slot} {day}: nothing to send")
    else:
        _print_parts(f"[preview] slot {slot} {day}: {digest.item_count} item(s), {len(parts)} message(s)", parts)
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.slot is not None:
        parse_slot(args.slot)
    if args.send:
        if not cfg.notify.enabled:
            raise ConfigError("notify.enabled is false: nothing is sent. Set `notify.enabled: true` in .config.yaml")
        sender: Sender | None = make_sender(cfg)
    else:
        sender = None
    path = config.db_path()
    if not path.exists():
        raise ConfigError(f"no database at {path}; run `repka db migrate` first")
    domains = [d for d in load_domains() if d.enabled]
    now = datetime.now(UTC)
    conn = connect() if args.send else connect(readonly=True)  # preview cannot write, by construction
    try:
        out = run_digest(conn, domains, cfg, sender, now, send=args.send, slot=args.slot)
        if out.status == "not_due" and not args.send and args.slot is None:
            return _preview_anyway(conn, domains, cfg, now)
    finally:
        conn.close()
    return _report(out, args.slot)


def _report(out: DigestOutcome, forced_slot: str | None) -> int:
    where = f"slot {out.slot} {out.local_date}"
    if out.status == "not_due":
        why = f"slot {forced_slot} is already recorded for today" if forced_slot else "no digest slot is due"
        print(why)
        return 0
    if out.status == "empty":
        print(f"{where}: nothing to send")
        return 0
    if out.status == "preview":
        _print_parts(f"[preview] {where}: {out.item_count} item(s), {len(out.parts)} message(s)", out.parts)
        return 0
    if out.status == "failed":
        print(f"error: send failed for {where}: {out.error}; the slot stays open for the next run", file=sys.stderr)
        return 1
    print(f"sent {where}: {out.item_count} item(s) in {len(out.parts)} message(s)")
    if out.error:
        print(f"warning: a later message failed: {out.error}", file=sys.stderr)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    digest = sub.add_parser(
        "digest",
        help="build the digest and print it (preview); --send delivers it to Telegram",
        description="Build the digest of pending events and print it as Telegram HTML. The default and --dry-run "
        "only preview: nothing is sent and nothing is written. --send delivers it and records the slot; "
        "it needs `notify.enabled: true` in .config.yaml.",
    )
    digest.add_argument("--slot", metavar="HH:MM", help="force this slot of today instead of the one that is due")
    mode = digest.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true", help="send via Telegram and record the slot")
    mode.add_argument("--dry-run", action="store_true", help="preview only (this is the default)")
    digest.set_defaults(func=_cmd_digest)
