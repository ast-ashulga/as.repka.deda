"""`repka dashboard`: loopback binding, the missing-dependency error, and no SQL in the pages."""

import sys
from pathlib import Path

import pytest

import repka
from repka import dashboard_cli
from repka.cli import main


def test_missing_streamlit_exits_2_with_install_hint(home, monkeypatch, capsys):
    monkeypatch.setattr(dashboard_cli, "_streamlit_installed", lambda: False)
    assert main(["dashboard"]) == 2
    assert "uv sync --group dashboard" in capsys.readouterr().err


def test_launch_binds_loopback_only_with_telemetry_off(home, monkeypatch):
    monkeypatch.setattr(dashboard_cli, "_streamlit_installed", lambda: True)
    launched = []

    def fake_execv(path, argv):
        launched.append((path, argv))
        raise SystemExit(0)  # stands in for exec: the test must not replace the process

    monkeypatch.setattr(dashboard_cli.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        main(["dashboard", "--port", "8599"])
    path, argv = launched[0]
    assert path == sys.executable
    assert argv[1:4] == ["-m", "streamlit", "run"]
    assert Path(argv[4]).is_file()  # the app lives in the checkout, not in REPKA_HOME
    options = dict(zip(argv[5::2], argv[6::2], strict=False))
    assert options["--server.address"] == "127.0.0.1"
    assert options["--server.port"] == "8599"
    assert options["--server.headless"] == "true"
    assert options["--browser.gatherUsageStats"] == "false"


def test_pages_hold_no_sql():
    app_dir = Path(repka.__file__).resolve().parents[2] / "dashboard"
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8").upper()
        assert "SELECT " not in text and ".EXECUTE(" not in text, path
