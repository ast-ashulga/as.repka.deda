"""`repka dashboard [--port 8501]`: the local Streamlit dashboard (ADR-008), bound to 127.0.0.1."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

import repka
from repka.config import ConfigError

APP = Path(repka.__file__).resolve().parents[2] / "dashboard" / "app.py"
EPILOG = """\
Serves a read-only view of .state/repka.db (or $REPKA_HOME/.state/repka.db) on http://127.0.0.1:<port>.
Needs the optional dependencies: uv sync --group dashboard. Nothing is sent anywhere; Ctrl-C stops it.
Exit codes: 2 when streamlit is not installed or the port is invalid; otherwise the server's exit code.
"""


def _streamlit_installed() -> bool:
    return importlib.util.find_spec("streamlit") is not None


def _dashboard(args: argparse.Namespace) -> int:
    if not _streamlit_installed():
        raise ConfigError("streamlit is not installed (the dashboard needs it); run: uv sync --group dashboard")
    if not 1 <= args.port <= 65535:
        raise ConfigError(f"--port must be 1-65535, got {args.port}")
    if not APP.is_file():
        raise ConfigError(f"dashboard app not found at {APP}; run repka from a source checkout")
    argv = [
        sys.executable, "-m", "streamlit", "run", str(APP),
        "--server.address", "127.0.0.1",
        "--server.port", str(args.port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    print(f"repka dashboard: http://127.0.0.1:{args.port} (Ctrl-C to stop)", flush=True)
    os.execv(sys.executable, argv)  # replaces this process: signals reach streamlit directly


def register(sub: argparse._SubParsersAction) -> None:
    cmd = sub.add_parser(
        "dashboard",
        help="open the local dashboard (read-only, 127.0.0.1)",
        description="Start the Streamlit dashboard on 127.0.0.1 and print its URL.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cmd.add_argument("--port", type=int, default=8501, help="port to listen on (default: 8501)")
    cmd.set_defaults(func=_dashboard)
