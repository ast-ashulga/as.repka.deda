"""Dependency-stack extraction from key files. Pure functions; unparseable input never raises."""

from __future__ import annotations

import json
import logging
import re
import tomllib
from pathlib import PurePosixPath

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 1_000_000  # key files come from untrusted repos: refuse to parse anything larger

_REQ_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_GO_REQUIRE_LINE = re.compile(r"^\s*(\S+)\s+v\S+")


def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(spec: str) -> str | None:
    """Name part of a PEP 508 string: `pydantic[email]>=2; python_version>'3.8'` -> `pydantic`."""
    match = _REQ_NAME.match(spec)
    return _pep503(match.group(1)) if match else None


def _pyproject(text: str) -> set[str]:
    data = tomllib.loads(text)
    names: set[str] = set()
    for spec in (data.get("project") or {}).get("dependencies") or []:
        name = _requirement_name(spec) if isinstance(spec, str) else None
        if name:
            names.add(name)
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    names.update(_pep503(key) for key in poetry if key.lower() != "python")
    return names


def _package_json(text: str) -> set[str]:
    data = json.loads(text)
    names: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        names.update(key.lower() for key in (data.get(section) or {}))
    return names


def _cargo(text: str) -> set[str]:
    data = tomllib.loads(text)
    return {key.lower() for key in (data.get("dependencies") or {})}


def _go_mod(text: str) -> set[str]:
    names: set[str] = set()
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if in_block:
            if line == ")":
                in_block = False
                continue
            match = _GO_REQUIRE_LINE.match(line)
            if match:
                names.add(match.group(1).lower())
        elif re.fullmatch(r"require\s*\(", line):
            in_block = True
        elif line.startswith("require "):
            match = _GO_REQUIRE_LINE.match(line[len("require "):])
            if match:
                names.add(match.group(1).lower())
    return names


_PARSERS = {
    "pyproject.toml": _pyproject,
    "package.json": _package_json,
    "cargo.toml": _cargo,
    "go.mod": _go_mod,
}


def parse_deps(path: str, text: str) -> list[str]:
    """Sorted, unique, lowercase dependency names of a key file; `[]` (and a warning) if unparseable."""
    parser = _PARSERS.get(PurePosixPath(path).name.lower())
    if parser is None:
        return []
    if len(text) > MAX_TEXT_CHARS:
        log.warning("cannot parse %s: %d chars exceeds the %d limit", path, len(text), MAX_TEXT_CHARS)
        return []
    try:
        return sorted(parser(text))
    except Exception as exc:  # hostile input: syntax errors, odd shapes, RecursionError from deep nesting
        log.warning("cannot parse %s: %s", path, exc)
        return []
