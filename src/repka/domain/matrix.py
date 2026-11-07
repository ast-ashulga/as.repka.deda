"""Query matrix: a domain expanded into GitHub search queries, and the scan window.

Pure functions only (no network, no store). The collector appends its own date qualifier per slice;
DATE_RESERVE characters are kept free for it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from repka.config import CollectConfig, ConfigError
from repka.domain.schema import Domain, SearchSpec

MAX_QUERY_CHARS = 256
MAX_OPERATORS = 5
DATE_RESERVE = 30  # room for the collector's "pushed:..." qualifier


@dataclass(frozen=True)
class Query:
    key: str  # sha1(q)[:12]; stable across runs
    q: str  # without the date qualifier
    term: str  # keyword group ("a, b") or topic it came from
    language: str | None


@dataclass(frozen=True)
class Window:
    field: Literal["created", "pushed"]
    start: date
    end: date


def _quote(word: str) -> str:
    return f'"{word}"' if " " in word else word


def _in_fields(words: tuple[str, ...], spec: SearchSpec) -> tuple[str, ...]:
    """GitHub returns nothing for a parenthesised group with a quoted phrase when `in:` lists `name`
    (found in the T25 rehearsal), so `name` is dropped for such groups. Single words keep it."""
    if any(" " in w for w in words) and len(spec.in_fields) > 1:
        return tuple(f for f in spec.in_fields if f != "name")
    return spec.in_fields


def _keyword_q(words: tuple[str, ...], spec: SearchSpec, language: str | None) -> str:
    group = "(" + " OR ".join(_quote(w) for w in words) + ")"
    return " ".join([group, f"in:{','.join(_in_fields(words, spec))}", *_tail(spec, language)])


def _topic_q(topic: str, spec: SearchSpec, language: str | None) -> str:
    return " ".join([f"topic:{topic}", *_tail(spec, language)])


def _tail(spec: SearchSpec, language: str | None) -> list[str]:
    parts = []
    if language:
        parts.append(f"language:{_quote(language)}")
    if spec.min_stars > 0:
        parts.append(f"stars:>={spec.min_stars}")
    parts.append("archived:false")
    if spec.fork_policy == "include":
        parts.append("fork:true")
    elif spec.fork_policy == "only":
        parts.append("fork:only")
    parts.extend(f"NOT {_quote(word)}" for word in spec.exclude)
    return parts


def _fits(q: str, operators: int) -> bool:
    return operators <= MAX_OPERATORS and len(q) + DATE_RESERVE <= MAX_QUERY_CHARS


def _group_fits(words: tuple[str, ...], spec: SearchSpec, languages: tuple[str | None, ...]) -> bool:
    operators = len(words) - 1 + len(spec.exclude)
    return all(_fits(_keyword_q(words, spec, lang), operators) for lang in languages)


def _chunks(group: tuple[str, ...], spec: SearchSpec, languages: tuple[str | None, ...]) -> list[tuple[str, ...]]:
    """Greedily split a keyword group until every query built from it fits the limits."""
    chunks: list[tuple[str, ...]] = []
    current: tuple[str, ...] = ()
    for word in group:
        if _group_fits(current + (word,), spec, languages):
            current = current + (word,)
            continue
        if current:
            chunks.append(current)
        if not _group_fits((word,), spec, languages):
            raise ConfigError(
                f"search.keywords: {word!r} is too long or has too many operators for one GitHub query"
            )
        current = (word,)
    if current:
        chunks.append(current)
    return chunks


def build_queries(d: Domain) -> list[Query]:
    """Keyword groups first, then topics; each term is expanded per language (or once without languages)."""
    spec = d.search
    if len(spec.exclude) > MAX_OPERATORS:
        raise ConfigError(
            f"search.exclude has {len(spec.exclude)} words; GitHub allows at most {MAX_OPERATORS} operators per query"
        )
    languages: tuple[str | None, ...] = spec.languages or (None,)
    queries: list[Query] = []
    for group in spec.keywords:
        term = ", ".join(group)
        for chunk in _chunks(group, spec, languages):
            for lang in languages:
                queries.append(_make(_keyword_q(chunk, spec, lang), term, lang))
    for topic in spec.topics:
        for lang in languages:
            q = _topic_q(topic, spec, lang)
            if not _fits(q, len(spec.exclude)):
                raise ConfigError(f"search.topics: {topic!r} produces a query that is too long for GitHub")
            queries.append(_make(q, topic, lang))
    return queries


def _make(q: str, term: str, language: str | None) -> Query:
    return Query(key=hashlib.sha1(q.encode("utf-8")).hexdigest()[:12], q=q, term=term, language=language)


def scan_window(last_scan_at: datetime | None, now: datetime, cfg: CollectConfig) -> Window:
    """First scan: the backfill window (first_window). Later scans: from last scan minus overlap.

    Always filters on `pushed` so long-established but active repos are found on the first scan (RF8).
    """
    start = now - cfg.first_window if last_scan_at is None else last_scan_at - cfg.overlap
    return Window(field="pushed", start=start.date(), end=now.date())
