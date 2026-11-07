import logging
from pathlib import Path

from repka.github.stack import parse_deps

FIX = Path(__file__).parent / "fixtures" / "stack"


def _text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_pep621():
    assert parse_deps("pyproject.toml", _text("pyproject_pep621.toml")) == ["httpx", "pydantic", "typing-extensions"]


def test_parse_poetry_skips_python():
    assert parse_deps("pyproject.toml", _text("pyproject_poetry.toml")) == ["fast-mcp", "requests"]


def test_parse_package_json_includes_dev():
    assert parse_deps("package.json", _text("package.json")) == [
        "@modelcontextprotocol/sdk",
        "express",
        "typescript",
    ]


def test_parse_cargo():
    assert parse_deps("Cargo.toml", _text("Cargo.toml")) == ["serde", "tokio"]


def test_parse_go_mod():
    assert parse_deps("go.mod", _text("go.mod")) == [
        "github.com/spf13/cobra",
        "github.com/stretchr/testify",
        "golang.org/x/sys",
    ]


def test_parse_broken_returns_empty(caplog):
    with caplog.at_level(logging.WARNING, logger="repka.github.stack"):
        assert parse_deps("pyproject.toml", _text("broken.toml")) == []
        assert parse_deps("package.json", "{not json") == []
    assert "cannot parse" in caplog.text


def test_hostile_input_returns_empty_without_raising():
    deep = "[" * 100_000 + "]" * 100_000
    assert parse_deps("package.json", deep) == []  # RecursionError inside json
    assert parse_deps("pyproject.toml", "a = " + deep) == []
    assert parse_deps("go.mod", "require x v1\n" * 200_000) == []  # over the size cap
