"""`repka dashboard [--port 8501]`: the local Streamlit dashboard, bound to 127.0.0.1.

The dashboard files live in the source checkout (`dashboard/` next to `src/`), so this command needs a
checkout or an editable install; a regular wheel install does not ship them.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

import repka
from repka.config import ConfigError

# Only valid for a source checkout or an editable install (src/repka/ -> checkout root); _dashboard checks it.
APP = Path(repka.__file__).resolve().parents[2] / "dashboard" / "app.py"
EPILOG = """\
Serves a view of .state/repka.db (or $REPKA_HOME/.state/repka.db) on http://127.0.0.1:<port>.
It reads the database without writing, except: the Domains page creates, edits and deletes domains/*.yaml, and
the Landscape "Recompute" button applies pending migrations and stores the map in the database.
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
        raise ConfigError(
            f"dashboard app not found at {APP}; the dashboard needs a source checkout "
            "(run `uv run repka dashboard` inside the clone, or install with `pip install -e .`)"
        )
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
        help="open the local dashboard (127.0.0.1; edits domain files, Landscape stores a map)",
        description="Start the Streamlit dashboard on 127.0.0.1 and print its URL.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cmd.add_argument("--port", type=int, default=8501, help="port to listen on (default: 8501)")
    cmd.set_defaults(func=_dashboard)
