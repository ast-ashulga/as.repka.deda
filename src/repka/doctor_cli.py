"""`repka doctor [--send] [--json]`: is this machine ready for `repka run`? One line per check."""

from __future__ import annotations

import argparse
import json

from repka import preflight
from repka.config import load_config

EPILOG = """\
One line per check: `ok`, `warn` or `error`, the check id, what was found and, for a problem, the exact fix.
Errors would stop `repka run`; warnings would not. After the lines, a numbered to-do list repeats the problems
(errors first). Reads files only; the one network call is GET /user to verify the GitHub token (a network
failure is a warning). No model is loaded and nothing is sent.
Exit 0 when there are no errors (warnings are fine), 2 when something must be fixed.
--json prints {"ok", "errors", "warnings", "checks": [{"id", "status", "title", "fix"}]} on stdout.
"""


def _doctor(args: argparse.Namespace) -> int:
    cfg = load_config()
    send = cfg.run.send or args.send
    results = preflight.diagnose(cfg, send=send)
    findings = preflight.findings_of(results)
    errors = sum(f.level == "error" for f in findings)
    warnings = len(findings) - errors
    if args.json:
        payload = {
            "ok": errors == 0,
            "errors": errors,
            "warnings": warnings,
            "checks": [{"id": r.id, "status": r.status, "title": r.title, "fix": r.fix} for r in results],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(preflight.format_results(results))
        if findings:
            print(f"\n{errors} error(s), {warnings} warning(s). To do:\n{preflight.format_findings(findings)}")
        else:
            print("\nall good")
    return 2 if errors else 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "doctor",
        help="check that repka is ready to run: token, domains, models, Telegram; prints the fixes",
        description="List everything `repka run` needs, what is missing, and the command that fixes each problem.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--send", action="store_true", help="also check what sending the digest needs (as `repka run --send`)")
    p.add_argument("--json", action="store_true", help="print the checks as JSON on stdout")
    p.set_defaults(func=_doctor)
