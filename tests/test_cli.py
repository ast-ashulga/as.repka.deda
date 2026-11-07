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
