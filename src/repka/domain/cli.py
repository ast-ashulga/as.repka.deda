"""`repka domain` subcommands: `list`, `show` and `plan`. `new` is added by a later task."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timezone

from repka.config import domains_dir, load_config
from repka.domain.matrix import build_queries, scan_window
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

    plan = actions.add_parser("plan", help="print the GitHub search queries and the first-scan window")
    plan.add_argument("id", help="domain id, e.g. example-mcp")
    plan.add_argument("--json", action="store_true", help="print JSON instead of a numbered list")
    plan.set_defaults(func=_cmd_plan)


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


def _cmd_plan(args: argparse.Namespace) -> int:
    d = get_domain(args.id)
    queries = build_queries(d)
    # No store lookup here: the plan shows the window a domain's first scan would use.
    window = scan_window(None, datetime.now(timezone.utc), load_config().collect)
    if args.json:
        payload = {
            "domain": d.id,
            "queries": [dataclasses.asdict(q) for q in queries],
            "window": {"field": window.field, "start": window.start.isoformat(), "end": window.end.isoformat()},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    for number, q in enumerate(queries, 1):
        language = f"  [{q.language}]" if q.language else ""
        print(f"{number:>3}. {q.key}{language}  {printable(q.q)}")
    print(f"window: {window.field} {window.start.isoformat()}..{window.end.isoformat()} (first scan)")
    return 0
