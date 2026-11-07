"""`repka domain` subcommands. `list` and `show` are here; `plan` and `new` are added by later tasks."""

from __future__ import annotations

import argparse
import sys

from repka.config import domains_dir
from repka.domain.schema import dump_domain, format_duration, get_domain, load_domains
from repka.textutil import printable


def register(sub: argparse._SubParsersAction) -> None:
    domain = sub.add_parser("domain", help="define and inspect monitoring domains")
    domain.set_defaults(func=lambda args: _print_help(domain))
    actions = domain.add_subparsers(dest="domain_command", metavar="COMMAND")

    listing = actions.add_parser("list", help="list domains: id, enabled state, scan interval")
    listing.set_defaults(func=_cmd_list)

    show = actions.add_parser("show", help="print one domain file as validated YAML")
    show.add_argument("id", help="domain id, e.g. example-mcp")
    show.set_defaults(func=_cmd_show)


def _print_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    domains = load_domains()
    if not domains:
        print(f"no domains in {domains_dir()}", file=sys.stderr)
        return 0
    width = max(len(d.id) for d in domains)
    for d in domains:
        state = "enabled" if d.enabled else "disabled"
        print(f"{d.id:<{width}}  {state:<8}  {format_duration(d.scan_every)}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    print(printable(dump_domain(get_domain(args.id))), end="")
    return 0
