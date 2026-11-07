"""repka command line.

Results go to stdout; progress, logs and errors go to stderr as `error: <message>`.
Exit codes: 0 success, 1 runtime failure (GitHub, worker), 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from repka import __version__
from repka.config import load_config
from repka.digest import cli as digest_cli
from repka.domain import cli as domain_cli
from repka.logging_setup import setup_logging
from repka.notify import cli as notify_cli
from repka.store import cli as store_cli

DESCRIPTION = (
    "repka: monitor GitHub repositories in user-defined technology domains, "
    "triage them with local deciders and send Telegram digests."
)
EPILOG = """\
exit codes: 0 ok | 1 runtime failure (GitHub, worker) | 2 usage or configuration error
output:     results on stdout; progress, logs and `error: <message>` on stderr
config:     .config.yaml (tracked defaults), .env (secrets), domains/*.yaml; see CLAUDE.md
"""


class _Parser(argparse.ArgumentParser):
    """Usage errors print `error: <message>` (like every other repka error) and exit 2."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="repka",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"repka {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="also log DEBUG messages to stderr")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    # Subcommands register here: each module exposes register(sub) and is called from this function.
    domain_cli.register(sub)
    store_cli.register(sub)
    notify_cli.register(sub)
    digest_cli.register(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config()
        setup_logging(cfg.run.log_level, verbose=args.verbose)
        func = getattr(args, "func", None)
        if func is None:
            parser.print_help()
            return 0
        return int(func(args))
    except ValueError as exc:  # ConfigError is a ValueError
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
