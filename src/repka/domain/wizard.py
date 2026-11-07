"""`repka domain new`: interview the user and build a valid Domain.

`run_wizard` only builds and returns the Domain (or None if the user declines to save); the caller writes it.
It never enables a domain unless the user says yes, and touches the network only through `probe`.
Prompts go to stderr, so stdout stays for results. Answers can be piped in, one per line.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Protocol, TypeVar

from repka.config import ConfigError, domains_dir, parse_duration
from repka.domain.matrix import build_queries
from repka.domain.schema import (
    FORK_POLICIES,
    ID_PATTERN,
    MIN_SCAN_EVERY,
    Domain,
    SearchSpec,
)
from repka.domain.suggest import suggest_keywords
from repka.textutil import printable

DEFAULT_MIN_STARS = 5
_QUOTES = "\"'«»`"
T = TypeVar("T")


class WizardIO(Protocol):
    def ask(self, prompt: str, default: str = "") -> str: ...

    def ask_multiline(self, prompt: str) -> str: ...

    def confirm(self, prompt: str, default: bool = False) -> bool: ...

    def echo(self, text: str) -> None: ...


class TerminalIO:
    """stdin/stderr implementation. Works the same on a terminal and on a pipe."""

    def ask(self, prompt: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        text = self._read(f"{prompt}{hint}: ")
        return text.strip() or default

    def ask_multiline(self, prompt: str) -> str:
        lines: list[str] = []
        sys.stderr.write(f"{prompt} (finish with an empty line):\n")
        sys.stderr.flush()
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip():
                if lines:
                    break
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def confirm(self, prompt: str, default: bool = False) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        text = self._read(f"{prompt} {hint}: ").strip().lower()
        if not text:
            return default
        return text in ("y", "yes")

    def echo(self, text: str) -> None:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()

    @staticmethod
    def _read(prompt: str) -> str:
        sys.stderr.write(prompt)
        sys.stderr.flush()
        try:
            return input()
        except EOFError:
            raise ConfigError("input ended before the wizard finished; nothing was saved") from None


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug if len(slug) >= 2 else ""


def _ask_valid(io: WizardIO, prompt: str, default: str, parse: Callable[[str], T]) -> T:
    """Ask until `parse` accepts the answer; a rejected answer is shown and asked again."""
    while True:
        answer = io.ask(prompt, default)  # input errors (end of input) propagate; only bad answers re-ask
        try:
            return parse(answer)
        except ConfigError as exc:
            io.echo(f"  error: {exc}")


def _terms(text: str) -> list[str]:
    """`a, b` -> ['a', 'b']; surrounding quotes are dropped so the query builder does not double them."""
    terms = []
    for raw in text.split(","):
        term = raw.strip().strip(_QUOTES).strip()
        if term:
            terms.append(term)
    return terms


def _groups(text: str) -> list[tuple[str, ...]]:
    """`a, b; c, d` -> [('a','b'), ('c','d')]. A single '-' means no keyword groups."""
    if text.strip() == "-":
        return []
    groups = [tuple(_terms(part)) for part in text.split(";")]
    return [group for group in groups if group]


def _format_groups(groups: Sequence[Sequence[str]]) -> str:
    """Inverse of _groups: suggestions shown as the default must parse back to the same groups."""
    return "; ".join(", ".join(group) for group in groups)


def _parse_name(text: str) -> str:
    if not text.strip():
        raise ConfigError("the name must not be empty")
    return text.strip()


def _parse_id(text: str) -> str:
    if not ID_PATTERN.fullmatch(text):
        raise ConfigError("id must be 2-40 characters of a-z, 0-9 and '-', starting with a letter or digit")
    return text


def _parse_min_stars(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise ConfigError(f"min stars must be a whole number (got {text!r})") from None
    if value < 0:
        raise ConfigError("min stars must be 0 or more")
    return value


def _parse_fork_policy(text: str) -> str:
    if text not in FORK_POLICIES:
        raise ConfigError(f"fork policy must be one of {', '.join(FORK_POLICIES)} (got {text!r})")
    return text


def _parse_scan_every(text: str) -> timedelta:
    try:
        duration = parse_duration(text)
    except ConfigError as exc:
        raise ConfigError(str(exc)) from None
    if duration < MIN_SCAN_EVERY:
        raise ConfigError(f"scan every must be at least 1h (got {text!r})")
    return duration


def _list(text: str) -> tuple[str, ...]:
    return tuple(_terms(text))


def run_wizard(
    io: WizardIO,
    description: str | None = None,
    probe: Callable[[str], int] | None = None,
) -> Domain | None:
    """Interview in order and return the new Domain, or None if the user does not confirm the save.

    `probe(q)` gets each query's `q` (no date qualifier) and returns its GitHub total_count; it is called
    only if the user confirms the probe step. The wizard never enables the domain unless asked to.
    """
    io.echo("repka domain new: answer each question; Enter takes the default in brackets.")
    name = _ask_valid(io, "Domain name", "", _parse_name)
    base = domains_dir()

    while True:
        domain_id = _ask_valid(io, "Domain id", _slug(name), _parse_id)
        if not (base / f"{domain_id}.yaml").is_file():
            break
        io.echo(f"  domain {domain_id!r} already exists in {base}")
        if io.confirm("Overwrite the existing file?", default=False):
            break

    text = description if description is not None else io.ask_multiline("Describe the domain (what is in and out)")
    if not text.strip():
        raise ConfigError("a domain description is required (use --from-file to pass one)")
    profile = text.strip()

    suggestions = suggest_keywords(profile)
    if suggestions:
        io.echo("Suggested keyword groups: " + _format_groups(suggestions))
    while True:
        keyword_groups = _ask_valid(
            io,
            "Keyword groups (terms ORed inside a group, groups separated by ';'; '-' for none)",
            _format_groups(suggestions),
            _groups,
        )
        topics = _ask_valid(io, "GitHub topics (comma-separated, Enter for none)", "", _list)
        if keyword_groups or topics:
            break
        io.echo("  error: at least one keyword group or topic is required")
    languages = _ask_valid(io, "Languages (comma-separated, Enter for any)", "", _list)
    excludes = _ask_valid(io, "Words to exclude (comma-separated, Enter for none)", "", _list)
    min_stars = _ask_valid(io, "Minimum stars", str(DEFAULT_MIN_STARS), _parse_min_stars)
    fork_policy = _ask_valid(io, "Forks: exclude, include or only", "exclude", _parse_fork_policy)
    scan_every = _ask_valid(io, "Scan every (e.g. 12h, 7d; at least 1h)", "12h", _parse_scan_every)
    enabled = io.confirm("Enable this domain now?", default=False)

    domain = Domain(
        id=domain_id,
        name=name,
        enabled=enabled,
        scan_every=scan_every,
        profile=profile,
        search=SearchSpec(
            keywords=tuple(keyword_groups),
            topics=tuple(topics),
            languages=tuple(languages),
            exclude=tuple(excludes),
            min_stars=min_stars,
            fork_policy=fork_policy,
        ),
    )

    queries = build_queries(domain)  # ConfigError here names the offending term; nothing is saved
    io.echo(f"{len(queries)} queries:")
    for number, query in enumerate(queries, 1):
        language = f"  [{query.language}]" if query.language else ""
        io.echo(f"{number:>3}. {query.key}{language}  {printable(query.q)}")

    if probe is not None and io.confirm("Count GitHub results per query now? (network calls)", default=False):
        for number, query in enumerate(queries, 1):
            io.echo(f"{number:>3}. {query.key}  total_count={probe(query.q)}")

    if not io.confirm(f"Save domains/{domain_id}.yaml?", default=False):
        return None
    return domain
