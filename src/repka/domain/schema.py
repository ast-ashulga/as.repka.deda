"""Domain files `domains/<id>.yaml`: strict loading, atomic saving, and the profile hash.

Same rules as config.py: unknown keys and wrong types raise ConfigError naming the dotted key.
A domain's `enabled` is a domain-level switch and defaults to False.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

from repka.config import ConfigError, domains_dir, parse_duration

ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,39}")
FORK_POLICIES = ("exclude", "include", "only")
SEARCH_FIELDS = ("name", "description", "readme")
MIN_SCAN_EVERY = timedelta(hours=1)
DEFAULT_SCAN_EVERY = timedelta(hours=12)
TOP_KEYS = frozenset({"id", "name", "enabled", "scan_every", "profile", "search", "key_files", "triage"})
SEARCH_KEYS = frozenset({"keywords", "topics", "languages", "exclude", "min_stars", "in", "fork_policy"})


@dataclass(frozen=True)
class SearchSpec:
    keywords: tuple[tuple[str, ...], ...]  # each group is ORed inside; one query per group (groups are never combined)
    topics: tuple[str, ...]
    languages: tuple[str, ...]  # () = no language qualifier
    exclude: tuple[str, ...]
    min_stars: int = 0
    in_fields: tuple[str, ...] = ("name", "description", "readme")
    fork_policy: Literal["exclude", "include", "only"] = "exclude"


@dataclass(frozen=True, kw_only=True)
class Domain:
    id: str
    name: str
    enabled: bool = False
    scan_every: timedelta = DEFAULT_SCAN_EVERY
    profile: str
    search: SearchSpec
    key_files: tuple[str, ...] = ()
    triage_questions: dict[str, Any] = field(default_factory=dict)


def _fail(key: str, message: str) -> ConfigError:
    return ConfigError(f"{key} {message}")


def _known_keys(section: str, raw: dict[str, Any], allowed: frozenset[str]) -> None:
    for name in raw:
        if name not in allowed:
            where = f"{section}.{name}" if section else str(name)
            raise ConfigError(f"unknown config key '{where}'")


def _text(key: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(key, f"must be a non-empty string (got {value!r})")
    return value.strip()


def _str_items(key: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise _fail(key, f"must be a list of non-empty strings (got {value!r})")
    return tuple(v.strip() for v in value)


def _keyword_groups(key: str, value: Any) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _fail(key, f"must be a list of keyword groups (got {value!r})")
    groups = []
    for index, group in enumerate(value):
        items = _str_items(f"{key}[{index}]", group)
        if not items:
            raise _fail(f"{key}[{index}]", "must not be empty")
        groups.append(items)
    return tuple(groups)


def _bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _fail(key, f"must be true or false (got {value!r})")
    return value


def _non_negative_int(key: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail(key, f"must be an integer of 0 or more (got {value!r})")
    return value


def _scan_every(key: str, value: Any) -> timedelta:
    try:
        duration = parse_duration(value)
    except ConfigError as exc:
        raise ConfigError(f"{key}: {exc}") from None
    if duration < MIN_SCAN_EVERY:
        raise _fail(key, f"must be at least 1h (got {value!r})")
    return duration


def _search(raw: Any) -> SearchSpec:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _fail("search", f"must be a mapping of keys (got {raw!r})")
    _known_keys("search", raw, SEARCH_KEYS)
    keywords = _keyword_groups("search.keywords", raw.get("keywords"))
    topics = _str_items("search.topics", raw.get("topics"))
    if not keywords and not topics:
        raise ConfigError("search: at least one keyword group or topic is required")
    fork_policy = raw.get("fork_policy", "exclude")
    if fork_policy not in FORK_POLICIES:
        raise _fail("search.fork_policy", f"must be one of {', '.join(FORK_POLICIES)} (got {fork_policy!r})")
    in_fields = _str_items("search.in", raw.get("in", list(SEARCH_FIELDS)))
    if not in_fields or not set(in_fields) <= set(SEARCH_FIELDS):
        raise _fail("search.in", f"must be a non-empty list of {', '.join(SEARCH_FIELDS)} (got {in_fields!r})")
    return SearchSpec(
        keywords=keywords,
        topics=topics,
        languages=_str_items("search.languages", raw.get("languages")),
        exclude=_str_items("search.exclude", raw.get("exclude")),
        min_stars=_non_negative_int("search.min_stars", raw.get("min_stars", 0)),
        in_fields=in_fields,
        fork_policy=fork_policy,
    )


def _check_id(domain_id: Any) -> None:
    if not isinstance(domain_id, str) or not ID_PATTERN.fullmatch(domain_id):
        raise ConfigError(
            f"domain id {domain_id!r} must be 2-40 characters of a-z, 0-9 and '-', starting with a letter or digit"
        )


def _domain_path(domain_id: str, directory: Path | None) -> Path:
    _check_id(domain_id)  # also keeps the id from escaping the domains directory
    base = domains_dir() if directory is None else directory
    return base / f"{domain_id}.yaml"


def format_duration(value: timedelta) -> str:
    """timedelta -> '12h' / '7d' / '30m' (the largest whole unit)."""
    seconds = int(value.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    raise ConfigError(f"duration {value} is not a whole number of minutes")


def load_domain(path: Path) -> Domain:
    """Load and validate one domain file. The file stem must equal the domain id."""
    if not path.is_file():
        raise ConfigError(f"no domain file {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} must be a mapping of keys")
    _known_keys("", raw, TOP_KEYS)

    domain_id = _text("id", raw.get("id")) if "id" in raw else _required("id")
    if domain_id != path.stem:
        raise ConfigError(f"id {domain_id!r} must equal the file name ({path.stem}.yaml)")
    _check_id(domain_id)

    triage = raw.get("triage") or {}
    if not isinstance(triage, dict):
        raise _fail("triage", f"must be a mapping of keys (got {triage!r})")
    _known_keys("triage", triage, frozenset({"questions"}))
    questions = triage.get("questions") or {}
    if not isinstance(questions, dict):
        raise _fail("triage.questions", f"must be a mapping (got {questions!r})")

    return Domain(
        id=domain_id,
        name=_text("name", raw["name"]) if "name" in raw else _required("name"),
        enabled=_bool("enabled", raw["enabled"]) if "enabled" in raw else False,
        scan_every=_scan_every("scan_every", raw["scan_every"]) if "scan_every" in raw else DEFAULT_SCAN_EVERY,
        profile=_text("profile", raw["profile"]) if "profile" in raw else _required("profile"),
        search=_search(raw.get("search")) if "search" in raw else _required("search"),
        key_files=_str_items("key_files", raw.get("key_files")),
        triage_questions=dict(questions),
    )


def _required(key: str) -> Any:
    raise ConfigError(f"{key} is required")


def load_domains(directory: Path | None = None) -> list[Domain]:
    """Every `*.yaml` in the directory (default: <repka home>/domains), sorted by id."""
    base = domains_dir() if directory is None else directory
    if not base.is_dir():
        return []
    return sorted((load_domain(path) for path in base.glob("*.yaml")), key=lambda d: d.id)


def get_domain(domain_id: str) -> Domain:
    path = _domain_path(domain_id, None)
    if not path.is_file():
        raise ConfigError(f"no domain {domain_id!r} in {path.parent} (see: repka domain list)")
    return load_domain(path)


def select_domains(domain_id: str | None) -> list[Domain]:
    """The named domain (runs even if it is not enabled), else every enabled domain."""
    if domain_id:
        return [get_domain(domain_id)]
    return [d for d in load_domains() if d.enabled]


class _DomainDumper(yaml.SafeDumper):
    """Multi-line strings (the profile) are written as literal blocks so the file stays readable."""


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)


_DomainDumper.add_representer(str, _str_representer)


def dump_domain(d: Domain) -> str:
    """The YAML text of a domain; load_domain(dump) reproduces the same Domain."""
    data = {
        "id": d.id,
        "name": d.name,
        "enabled": d.enabled,
        "scan_every": format_duration(d.scan_every),
        "profile": d.profile,
        "search": {
            "keywords": [list(group) for group in d.search.keywords],
            "topics": list(d.search.topics),
            "languages": list(d.search.languages),
            "exclude": list(d.search.exclude),
            "min_stars": d.search.min_stars,
            "in": list(d.search.in_fields),
            "fork_policy": d.search.fork_policy,
        },
        "key_files": list(d.key_files),
        "triage": {"questions": dict(d.triage_questions)},
    }
    return yaml.dump(data, Dumper=_DomainDumper, allow_unicode=True, sort_keys=False, width=100)


def save_domain(d: Domain, directory: Path | None = None) -> Path:
    """Write `<id>.yaml` atomically (temp file, then os.replace). Returns the path."""
    path = _domain_path(d.id, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(dump_domain(d), encoding="utf-8")
    os.replace(tmp, path)
    return path


def delete_domain(domain_id: str, directory: Path | None = None) -> None:
    path = _domain_path(domain_id, directory)
    if not path.is_file():
        raise ConfigError(f"no domain {domain_id!r} in {path.parent}")
    path.unlink()


def profile_hash(d: Domain) -> str:
    return hashlib.sha256(d.profile.encode("utf-8")).hexdigest()
