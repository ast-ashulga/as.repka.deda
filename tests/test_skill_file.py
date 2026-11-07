import argparse
import re
from pathlib import Path

import yaml

from repka.cli import build_parser
from repka.domain.schema import load_domain

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / ".claude" / "skills" / "domain-profile"
SKILL = SKILL_DIR / "SKILL.md"
TEMPLATE = SKILL_DIR / "domain-template.yaml"


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def test_frontmatter_has_name_and_description():
    text = SKILL.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md needs a YAML frontmatter block"
    meta = yaml.safe_load(match.group(1))
    assert meta["name"] == "domain-profile"
    assert meta["description"].startswith("Use when")


def test_every_repka_command_in_skill_exists():
    commands = _subparsers(build_parser())
    mentioned = re.findall(r"\brepka ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", SKILL.read_text(encoding="utf-8"))
    assert mentioned, "SKILL.md should show the repka commands it runs"
    for top, sub in mentioned:
        assert top in commands, f"repka {top} does not exist"
        if sub:
            assert sub in _subparsers(commands[top]), f"repka {top} {sub} does not exist"


def test_template_loads_as_domain(tmp_path):
    text = TEMPLATE.read_text(encoding="utf-8").replace("id: <domain-id>", "id: template")
    path = tmp_path / "template.yaml"
    path.write_text(text, encoding="utf-8")
    d = load_domain(path)
    assert d.id == "template"
    assert d.enabled is False
