"""`repka notify login` and `repka notify test`."""

from __future__ import annotations

import argparse
import os
import sys

from repka import config
from repka.config import ConfigError, load_config
from repka.notify.telegram_user import TelegramUserSender
from repka.textutil import html_escape, printable


def _login(_args: argparse.Namespace) -> int:
    env = config.telegram_server_env()  # validates REPKA_TG_API_ID / REPKA_TG_API_HASH first
    directory = config.session_dir()
    script = config.venv_script("fast-mcp-telegram-setup")
    print(f"Logging in; the session will be stored in {config.session_file()}", file=sys.stderr)
    # Replace this process so the phone / code / 2FA prompts get the real terminal.
    os.chdir(directory)
    os.execve(script, [script], env)
    return 0  # unreachable


def _test(args: argparse.Namespace) -> int:
    cfg = load_config().notify
    parts = [html_escape(printable(args.text))]
    if args.dry_run:
        print(f"[dry-run] to {cfg.chat_id}" + (f" topic {cfg.topic_id}" if cfg.topic_id is not None else ""))
        for i, part in enumerate(parts, 1):
            print(f"--- part {i}/{len(parts)} ---\n{part}")
        return 0
    if not cfg.enabled:
        raise ConfigError("notify.enabled is false: nothing is sent. Set `notify.enabled: true` in .config.yaml")
    result = TelegramUserSender(cfg.chat_id, cfg.topic_id).send(parts)
    if not result.ok:
        print(f"error: send failed after {result.sent} part(s): {result.error}", file=sys.stderr)
        return 1
    print(f"sent {result.sent} message(s) to {cfg.chat_id}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    notify = sub.add_parser("notify", help="Telegram delivery: one-time login and a test message")
    notify_sub = notify.add_subparsers(dest="notify_command", metavar="SUBCOMMAND")
    notify_sub.required = True

    login = notify_sub.add_parser(
        "login",
        help="one-time interactive Telegram login (phone, code, 2FA); needs REPKA_TG_API_ID/HASH in .env",
    )
    login.set_defaults(func=_login)

    test = notify_sub.add_parser(
        "test",
        help="send TEXT to notify.chat_id (only if notify.enabled: true)",
        description="Send TEXT from your own Telegram account to notify.chat_id. "
        "Refused unless notify.enabled is true; --dry-run prints the message and opens no session.",
    )
    test.add_argument("text", metavar="TEXT", help="message text (plain; it is HTML-escaped)")
    test.add_argument("--dry-run", action="store_true", help="print what would be sent; send nothing")
    test.set_defaults(func=_test)
