import os
import plistlib
import subprocess

import pytest

from repka import schedule
from repka.cli import main


class FakeLaunchctl:
    """Records launchctl calls instead of running them; `rc` maps a verb to a return code."""

    def __init__(self, rc: dict[str, int] | None = None):
        self.calls: list[list[str]] = []
        self.rc = rc or {}

    def __call__(self, args):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, self.rc.get(args[0], 0), stdout="", stderr="launchctl says no")


@pytest.fixture
def launchd(home, monkeypatch):
    """Isolated launchd: HOME and REPKA_HOME are temp dirs, launchctl and uv are fakes (never the real ones)."""
    fake_home = home / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    fake = FakeLaunchctl()
    monkeypatch.setattr(schedule, "_launchctl", fake)
    monkeypatch.setattr(schedule, "_find_uv", lambda: "/opt/uv/bin/uv")
    return fake


def test_plist_is_valid_and_has_no_secrets(home):
    xml = schedule.plist_xml("/opt/uv/bin/uv", home)
    data = plistlib.loads(xml.encode())
    assert data["Label"] == "com.repka.run"
    assert data["ProgramArguments"] == ["/opt/uv/bin/uv", "run", "--project", str(home), "repka", "run"]
    assert data["WorkingDirectory"] == str(home)
    assert data["StartInterval"] == 3600
    assert data["RunAtLoad"] is False
    assert data["StandardOutPath"] == str(home / "logs" / "launchd.log")
    assert data["StandardErrorPath"] == str(home / "logs" / "launchd.log")
    assert "EnvironmentVariables" not in data
    assert "TOKEN" not in xml


def test_install_writes_plist_and_bootstraps(launchd):
    schedule.install()
    path = schedule.plist_path()
    assert path.is_file()
    assert plistlib.loads(path.read_bytes())["StartInterval"] == 3600
    assert ["bootstrap", f"gui/{os.getuid()}", str(path)] in launchd.calls


def test_reinstall_boots_out_first(launchd):
    schedule.install()
    launchd.calls.clear()
    schedule.install()
    verbs = [c[0] for c in launchd.calls]
    assert verbs.index("bootout") < verbs.index("bootstrap")


def test_uninstall_removes_plist(launchd):
    schedule.install()
    schedule.uninstall()
    assert not schedule.plist_path().exists()
    assert ["bootout", f"gui/{os.getuid()}/com.repka.run"] in launchd.calls
    assert "not installed" in schedule.uninstall()  # idempotent


def test_dry_run_prints_plist_and_runs_nothing(launchd):
    text = schedule.install(dry_run=True)
    assert plistlib.loads(text.encode())["Label"] == "com.repka.run"
    assert launchd.calls == []
    assert not schedule.plist_path().exists()


def test_install_warns_when_sending_disabled(launchd, capsys):
    assert main(["schedule", "install", "--dry-run"]) == 0
    assert "send no digests" in capsys.readouterr().err


def test_bootstrap_failure_is_runtime_error(launchd):
    launchd.rc["bootstrap"] = 5
    with pytest.raises(schedule.ScheduleError):
        schedule.install()
