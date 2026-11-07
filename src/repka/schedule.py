"""`repka schedule install|uninstall|status`: one launchd user agent (`com.repka.run`) that runs `repka run`.

Nothing is installed automatically: only these explicit commands touch ~/Library/LaunchAgents or launchctl.
The plist carries no secrets: only PATH (launchd's default lacks the `gh` dir) and REPKA_HOME (when set at install);
`.env` is read by repka itself at run time.
launchctl verbs used: `bootstrap gui/<uid> <plist>`, `bootout gui/<uid>/<label>`, `print gui/<uid>/<label>`
(the documented launchctl(1) subcommands on macOS 10.10+; not exercised against a real agent in tests).
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from repka.config import load_config, logs_dir, parse_duration, project_root

LABEL = "com.repka.run"

Runner = Callable[[list[str]], subprocess.CompletedProcess]

EPILOG = """\
The agent runs `repka run` every INTERVAL (default 1h) with the repo as working directory. It does not run at
login, and it sends digests only when notify.enabled and run.send are both true.
"""


class ScheduleError(RuntimeError):
    """launchctl or the filesystem refused the change: a runtime failure (exit 1)."""


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchd_env() -> dict[str, str]:
    """PATH (current PATH plus the dir of `gh`, which launchd's /usr/bin:/bin default lacks) and REPKA_HOME if set."""
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    gh = shutil.which("gh")
    if gh:
        dirs.insert(0, str(Path(gh).parent))
    dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env = {"PATH": os.pathsep.join(dict.fromkeys(dirs))}
    if os.environ.get("REPKA_HOME"):
        env["REPKA_HOME"] = os.environ["REPKA_HOME"]
    return env


def plist_xml(uv_path: str, root: Path, interval_s: int = 3600, env: dict[str, str] | None = None) -> str:
    data = {
        "Label": LABEL,
        "ProgramArguments": [uv_path, "run", "--project", str(root), "repka", "run"],
        "WorkingDirectory": str(root),
        "StartInterval": interval_s,
        "RunAtLoad": False,
        "StandardOutPath": str(root / "logs" / "launchd.log"),
        "StandardErrorPath": str(root / "logs" / "launchd.log"),
    }
    if env:
        data["EnvironmentVariables"] = env
    return plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=False).decode()


def _launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def _find_uv() -> str | None:
    return shutil.which("uv")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target() -> str:
    return f"{_domain()}/{LABEL}"


def _loaded(run: Runner) -> bool:
    return run(["print", _target()]).returncode == 0


def install(
    run_cmd: Runner | None = None,
    dry_run: bool = False,
    *,
    interval_s: int = 3600,
    uv_path: str | None = None,
) -> str:
    """Write the plist and (re)load the agent. Returns the plist text for a dry run, else a one-line summary."""
    run = run_cmd or _launchctl
    uv = uv_path or _find_uv()
    if not uv:
        raise ScheduleError("uv not found on PATH: install uv (https://docs.astral.sh/uv/) and retry")
    root = project_root()
    xml = plist_xml(uv, root, interval_s, launchd_env())
    if dry_run:
        return xml
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(xml)
    run(["bootout", _target()])  # replace a previous agent; a first install has nothing to boot out
    result = run(["bootstrap", _domain(), str(path)])
    if result.returncode != 0:
        raise ScheduleError(f"launchctl bootstrap failed: {(result.stderr or '').strip() or result.returncode}")
    return f"installed {LABEL}: runs `repka run` every {interval_s}s ({path})"


def uninstall(run_cmd: Runner | None = None) -> str:
    run = run_cmd or _launchctl
    result = run(["bootout", _target()])
    if result.returncode != 0 and _loaded(run):
        raise ScheduleError(f"launchctl bootout failed: {(result.stderr or '').strip() or result.returncode}")
    path = plist_path()
    if path.exists():
        path.unlink()
        return f"uninstalled {LABEL} (removed {path})"
    return f"{LABEL} is not installed"


def status(run_cmd: Runner | None = None) -> str:
    run = run_cmd or _launchctl
    path = plist_path()
    result = run(["print", _target()])
    if result.returncode != 0:
        hint = " (plist exists but is not loaded: `repka schedule install`)" if path.exists() else ""
        return f"{LABEL}: not loaded{hint}"
    keys = ("state = ", "runs = ", "last exit code = ", "run interval = ")
    details = [line.strip() for line in (result.stdout or "").splitlines() if line.strip().startswith(keys)]
    return "\n".join([f"{LABEL}: loaded ({path})", *details])


def _cmd_install(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not (cfg.notify.enabled and cfg.run.send):
        print(
            "warning: runs will collect and triage but send no digests "
            "(notify.enabled and run.send must both be true to send)",
            file=sys.stderr,
        )
    interval_s = int(parse_duration(args.interval).total_seconds())
    try:
        print(install(dry_run=args.dry_run, interval_s=interval_s))
    except ScheduleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    try:
        print(uninstall())
    except ScheduleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    print(status())
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    schedule = sub.add_parser(
        "schedule",
        help="manage the launchd agent that runs `repka run` hourly",
        description="Install, remove or inspect the launchd user agent. Only these commands change it.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    actions = schedule.add_subparsers(dest="schedule_action", metavar="ACTION", required=True)

    install_p = actions.add_parser(
        "install",
        help="write ~/Library/LaunchAgents/com.repka.run.plist and load it (replaces an old one)",
    )
    install_p.add_argument("--dry-run", action="store_true", help="print the plist and change nothing")
    install_p.add_argument("--interval", default="1h", metavar="DURATION", help="run period, e.g. 30m or 1h (default 1h)")
    install_p.set_defaults(func=_cmd_install)

    uninstall_p = actions.add_parser("uninstall", help="unload the agent and remove its plist (idempotent)")
    uninstall_p.set_defaults(func=_cmd_uninstall)

    status_p = actions.add_parser("status", help="show whether the agent is loaded and its last exit code")
    status_p.set_defaults(func=_cmd_status)
