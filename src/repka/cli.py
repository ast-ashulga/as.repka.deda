"""repka command line.

Results go to stdout; progress, logs and errors go to stderr as `error: <message>`.
Exit codes: 0 success, 1 runtime failure (GitHub, worker), 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from repka import (
    __version__,
    collect_cli,
    dashboard_cli,
    doctor_cli,
    releases_cli,
    run_cli,
    schedule,
    status_cli,
)
from repka.config import ConfigError, load_config, logs_dir
from repka.digest import cli as digest_cli
from repka.domain import cli as domain_cli
from repka.embed import cli as embed_cli
from repka.logging_setup import setup_logging
from repka.notify import cli as notify_cli
from repka.store import cli as store_cli
from repka.triage import cli as triage_cli

DESCRIPTION = (
    "repka: monitor GitHub repositories in user-defined technology domains, "
    "triage them with local deciders and send Telegram digests."
)
EPILOG = """\
exit codes: 0 ok | 1 runtime failure (GitHub, worker) | 2 usage or configuration error
output:     results on stdout; progress, logs and `error: <message>` on stderr
config:     .config.yaml (settings), .env (secrets), domains/*.yaml; see docs/configuration.md
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
    collect_cli.register(sub)
    store_cli.register(sub)
    notify_cli.register(sub)
    digest_cli.register(sub)
    releases_cli.register(sub)
    triage_cli.register(sub)
    embed_cli.register(sub)
    run_cli.register(sub)
    status_cli.register(sub)
    doctor_cli.register(sub)
    schedule.register(sub)
    dashboard_cli.register(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:  # bare `repka`: help needs no configuration, so a broken one must not hide it
        parser.print_help()
        return 0
    try:
        try:
            cfg = load_config()
        except ConfigError:
            if args.command != "doctor":
                raise
            cfg = None  # `repka doctor` reports a broken configuration as a check, with the fix
        try:
            setup_logging(cfg.run.log_level if cfg else "INFO", verbose=args.verbose)
        except OSError as exc:  # unwritable REPKA_HOME or logs/: the user fixes the folder, not the code
            raise ConfigError(f"cannot write logs under {logs_dir()}: {exc.strerror or exc} (check REPKA_HOME)") from exc
        return int(func(args))
    except ConfigError as exc:  # the user can fix it
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # anything else is a runtime failure, not a usage error
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
