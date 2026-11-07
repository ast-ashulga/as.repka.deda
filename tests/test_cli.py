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
    assert main([]) == 2
    assert "error:" in capsys.readouterr().err


def test_non_config_value_error_is_a_runtime_failure(home, capsys, monkeypatch):
    from repka import collect_cli

    def boom(args):
        raise ValueError("vector blob is 3 bytes")

    monkeypatch.setattr(collect_cli, "_collect", boom)
    assert main(["collect", "--dry-run"]) == 1
    assert "error: vector blob" in capsys.readouterr().err
