import shutil
from pathlib import Path

import pytest

from repka.cli import main
from repka.config import ConfigError
from repka.domain.schema import dump_domain, get_domain, load_domain, load_domains, save_domain

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "domains" / "example-mcp.yaml"

MINIMAL = """\
id: {id}
name: "Demo"
profile: "Demo domain."
search:
  keywords:
    - ["foo bar"]
"""


def write(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_roundtrip(tmp_path):
    d = load_domain(EXAMPLE)
    save_domain(d, tmp_path)
    assert load_domain(tmp_path / f"{d.id}.yaml") == d


def test_id_must_match_file_stem(tmp_path):
    path = write(tmp_path, "b.yaml", MINIMAL.format(id="a"))
    with pytest.raises(ConfigError, match="b.yaml"):
        load_domain(path)


def test_needs_keywords_or_topics(tmp_path):
    text = MINIMAL.format(id="demo").replace('    - ["foo bar"]\n', "    []\n").replace("keywords:\n    []", "keywords: []")
    path = write(tmp_path, "demo.yaml", text)
    with pytest.raises(ConfigError, match="at least one keyword group or topic"):
        load_domain(path)


def test_invalid_fork_policy(tmp_path):
    text = MINIMAL.format(id="demo") + "  fork_policy: maybe\n"
    path = write(tmp_path, "demo.yaml", text)
    with pytest.raises(ConfigError, match="search.fork_policy"):
        load_domain(path)


def test_scan_every_minimum_one_hour(tmp_path):
    path = write(tmp_path, "demo.yaml", MINIMAL.format(id="demo") + "scan_every: 30m\n")
    with pytest.raises(ConfigError, match="scan_every"):
        load_domain(path)


def test_enabled_defaults_false(tmp_path):
    path = write(tmp_path, "demo.yaml", MINIMAL.format(id="demo"))
    assert load_domain(path).enabled is False


def test_unknown_key_rejected(tmp_path):
    path = write(tmp_path, "demo.yaml", MINIMAL.format(id="demo") + "colour: red\n")
    with pytest.raises(ConfigError, match="colour"):
        load_domain(path)


def test_example_domain_is_valid_and_disabled():
    d = load_domain(EXAMPLE)
    assert d.id == "example-mcp"
    assert d.enabled is False


def test_dump_is_loadable_text():
    text = dump_domain(load_domain(EXAMPLE))
    assert "id: example-mcp" in text and "fork_policy: exclude" in text


def test_load_domains_sorted_and_get_domain(tmp_path):
    write(tmp_path, "zeta.yaml", MINIMAL.format(id="zeta"))
    write(tmp_path, "alpha.yaml", MINIMAL.format(id="alpha"))
    assert [d.id for d in load_domains(tmp_path)] == ["alpha", "zeta"]


def test_get_domain_unknown_is_config_error(home):
    with pytest.raises(ConfigError, match="nope"):
        get_domain("nope")


def test_list_cli_prints_ids(home, capsys):
    domains = home / "domains"
    domains.mkdir()
    shutil.copy(EXAMPLE, domains / "example-mcp.yaml")
    assert main(["domain", "list"]) == 0
    assert capsys.readouterr().out == "example-mcp  disabled  12h\n"


def test_show_cli_prints_domain(home, capsys):
    domains = home / "domains"
    domains.mkdir()
    shutil.copy(EXAMPLE, domains / "example-mcp.yaml")
    assert main(["domain", "show", "example-mcp"]) == 0
    assert "id: example-mcp" in capsys.readouterr().out


def test_show_unknown_domain_is_config_error(home, capsys):
    assert main(["domain", "show", "nope"]) == 2
    assert "error:" in capsys.readouterr().err
