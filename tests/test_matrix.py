import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from repka.cli import main
from repka.config import CollectConfig, ConfigError
from repka.domain.matrix import (
    MAX_OPERATORS,
    MAX_QUERY_CHARS,
    build_queries,
    scan_window,
)
from repka.domain.schema import Domain, load_domain

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "domains" / "example-mcp.yaml"
NOW = datetime(2026, 11, 7, 8, 30, tzinfo=UTC)
DATE_RESERVE = 30  # chars the collector adds for its date qualifier


def domain_with(**search) -> Domain:
    base = load_domain(EXAMPLE)
    return replace(base, search=replace(base.search, **search))


def queries(**search):
    return build_queries(domain_with(**search))


def operators(q: str) -> int:
    return q.count(" OR ") + q.count(" NOT ")


def test_fork_policy_qualifier():
    assert "fork:" not in queries(fork_policy="exclude")[0].q
    assert "fork:true" in queries(fork_policy="include")[0].q
    assert "fork:only" in queries(fork_policy="only")[0].q


def test_terms_times_languages():
    qs = queries(
        keywords=(("model context protocol", "mcp server"), ("mcp client",)),
        topics=("mcp",),
        languages=("Python", "Go"),
        exclude=(),
    )
    assert [(q.term, q.language) for q in qs] == [
        ("model context protocol, mcp server", "Python"),
        ("model context protocol, mcp server", "Go"),
        ("mcp client", "Python"),
        ("mcp client", "Go"),
        ("mcp", "Python"),
        ("mcp", "Go"),
    ]
    assert qs[0].q.startswith('("model context protocol" OR "mcp server") in:description,readme')
    assert qs[4].q.startswith("topic:mcp")


def test_name_field_is_dropped_only_for_groups_with_phrases():
    # real GitHub returns 0 hits for ("a b" OR c) in:name,...; single-word groups are fine
    qs = queries(keywords=(("mcp server", "mcp"), ("mcp", "sdk")), topics=(), languages=("Python",))
    assert qs[0].q.startswith('("mcp server" OR mcp) in:description,readme ')
    assert qs[1].q.startswith("(mcp OR sdk) in:name,description,readme ")
    only_name = queries(keywords=(("mcp server",),), topics=(), languages=("Python",), in_fields=("name",))
    assert only_name[0].q.startswith('("mcp server") in:name ')


def test_no_language_qualifier_when_empty():
    qs = queries(languages=())
    assert qs and all(q.language is None and "language:" not in q.q for q in qs)


def test_operator_limit_splits_group():
    words = tuple(f"kw{i}" for i in range(8))
    qs = queries(keywords=(words,), topics=(), languages=(), exclude=("minecraft",))
    assert len(qs) > 1
    assert all(operators(q.q) <= MAX_OPERATORS for q in qs)
    assert all("NOT minecraft" in q.q for q in qs)


def test_query_length_limit():
    words = tuple("a fairly long keyword phrase number " + str(i) for i in range(12))
    qs = queries(keywords=(words,), topics=(), languages=("Python", "TypeScript"), exclude=())
    assert len(qs) > 1
    assert all(len(q.q) <= MAX_QUERY_CHARS - DATE_RESERVE for q in qs)


def test_keyword_too_long_raises():
    with pytest.raises(ConfigError, match="search.keywords"):
        queries(keywords=(("x" * 250,),))


def test_too_many_excludes_raises():
    with pytest.raises(ConfigError, match="search.exclude"):
        queries(exclude=tuple(f"word{i}" for i in range(MAX_OPERATORS + 1)))


def test_keys_stable_and_unique():
    first = queries()
    second = queries()
    assert [q.key for q in first] == [q.key for q in second]
    assert len({q.key for q in first}) == len(first)
    assert all(len(q.key) == 12 for q in first)


def test_window_first_scan_uses_pushed():
    w = scan_window(None, NOW, CollectConfig())
    assert (w.field, w.start) == ("pushed", (NOW - timedelta(days=365)).date())
    assert w.end == NOW.date()


def test_window_incremental_uses_pushed_with_overlap():
    last = NOW - timedelta(days=2)
    w = scan_window(last, NOW, CollectConfig())
    assert w.field == "pushed"
    assert w.start == (last - timedelta(days=1)).date()


def test_plan_json_cli(home, capsys):
    domains = home / "domains"
    domains.mkdir()
    shutil.copy(EXAMPLE, domains / "example-mcp.yaml")
    assert main(["domain", "plan", "example-mcp", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["domain"] == "example-mcp"
    assert payload["queries"] and set(payload["queries"][0]) == {"key", "q", "term", "language"}
    assert payload["window"]["field"] == "pushed"
    assert payload["window"]["start"] < payload["window"]["end"]
