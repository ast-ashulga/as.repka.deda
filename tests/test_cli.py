import pytest

from repka.cli import main


def test_help_exits_zero(capsys):
    for flag in ("--help", "--version"):
        with pytest.raises(SystemExit) as exc:
            main([flag])
        assert exc.value.code == 0
    assert "usage: repka" in capsys.readouterr().out


def test_config_error_exits_2(home, capsys):
    (home / ".config.yaml").write_text("github: [unclosed\n")
    assert main(["collect", "--dry-run"]) == 2
    assert "error:" in capsys.readouterr().err


def test_bare_repka_prints_help_even_with_a_broken_config(home, capsys):
    (home / ".config.yaml").write_text("github: [unclosed\n")
    assert main([]) == 0
    cap = capsys.readouterr()
    assert "usage: repka" in cap.out and "error" not in cap.err


def test_doctor_reports_a_broken_config_as_a_check(home, capsys):
    import json

    (home / ".config.yaml").write_text("github: [unclosed\n")
    assert main(["doctor"]) == 2
    cap = capsys.readouterr()
    assert "config" in cap.out and "1 error(s)" in cap.out and "Traceback" not in cap.err
    assert main(["doctor", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["checks"][0]["id"] == "config"


def test_non_config_value_error_is_a_runtime_failure(home, capsys, monkeypatch):
    from repka import collect_cli

    def boom(args):
        raise ValueError("vector blob is 3 bytes")

    monkeypatch.setattr(collect_cli, "_collect", boom)
    assert main(["collect", "--dry-run"]) == 1
    assert "error: vector blob" in capsys.readouterr().err


def test_unwritable_home_is_a_usage_error_not_a_traceback(home, capsys):
    (home / "logs").write_text("not a directory")  # logs/ cannot be created: setup_logging raises OSError
    assert main(["collect", "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: cannot write logs under ") and "REPKA_HOME" in err and "Traceback" not in err
