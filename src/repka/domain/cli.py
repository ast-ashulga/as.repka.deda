"""`repka domain` subcommands: `list`, `show`, `plan` and `new` (the interactive wizard)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from repka.config import ConfigError, domains_dir, github_token, github_token_source, load_config
from repka.domain.matrix import build_queries, scan_window
from repka.domain.schema import dump_domain, format_duration, get_domain, load_domains, save_domain
from repka.domain.wizard import TerminalIO, run_wizard
from repka.github.client import GitHubClient, GitHubError
from repka.textutil import printable

NEW_EPILOG = """\
answers, in order (one per line when piped; Enter takes the default; a blank line ends the description):
  name, id, description (skipped with --from-file), keyword groups ('-' for none), topics, languages,
  excludes, min stars, fork policy, scan interval, enable now (y/N), [probe (y/N)], save (y/N)
nothing is enabled unless you answer y; the file is written only after the final y.
"""


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

    new = actions.add_parser(
        "new",
        help="interview in the terminal and write domains/<id>.yaml",
        epilog=NEW_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    new.add_argument("--from-file", metavar="PATH", help="read the domain description from this file")
    new.add_argument(
        "--probe",
        action="store_true",
        help="after the plan, offer to count GitHub results per query (network; needs a token)",
    )
    new.set_defaults(func=_cmd_new)


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


def _cmd_new(args: argparse.Namespace) -> int:
    description = None
    if args.from_file:
        path = Path(args.from_file)
        if not path.is_file():
            raise ConfigError(f"no description file {path}")
        description = path.read_text(encoding="utf-8")

    client: GitHubClient | None = None
    probe = None
    if args.probe:
        cfg = load_config()
        token = github_token()  # fails now, before the interview, if there is no token
        client = GitHubClient(token, cfg.github, token_source=github_token_source())
        start = scan_window(None, datetime.now(timezone.utc), cfg.collect).start
        qualifier = f"pushed:>={start.isoformat()}"
        search = client.search_total_count

        def probe(q: str) -> int:
            return search(f"{q} {qualifier}")

    try:
        domain = run_wizard(TerminalIO(), description=description, probe=probe)
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
    if domain is None:
        print("not saved", file=sys.stderr)
        return 0
    print(save_domain(domain))
    print(f"next: repka domain plan {domain.id}", file=sys.stderr)
    return 0
