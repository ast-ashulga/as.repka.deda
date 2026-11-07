import re

from repka.digest.build import Digest, DigestSection
from repka.digest.render import render_digest, slot_label, split_messages
from repka.models import DigestItem


def item(line_kind, name="o/r", desc=None, **fields):
    return DigestItem("d1", 1, name, f"https://github.com/{name}", line_kind, {"description": desc, **fields}, 0.0)


def digest(**sections) -> Digest:
    return Digest("08:30", "2026-11-07", [DigestSection("d1", "MCP servers & tooling", **sections)], [], [])


def test_html_escaped():
    d = digest(new_repos=[item("new", "o/<b>x", "<script>&", stars=1200, kind="tool<i>", level=3)])
    text = "\n".join(render_digest(d))
    assert "&lt;script&gt;&amp;" in text
    assert "<script>" not in text
    assert "o/&lt;b&gt;x" in text
    assert "<b>MCP servers &amp; tooling</b>" in text
    assert set(re.findall(r"</?(\w+)", text)) <= {"b", "i", "a", "code"}  # Telegram-valid tags only


def test_render_matches_format():
    d = digest(
        new_repos=[
            item("new", "o/n", "d", stars=1200, kind="tool", level=3),
            item("found", "o/f", "d", stars=8400, kind="library", level=4, since=2023),
        ],
        releases=[item("release", tag="v1.2.0")],
        movers=[item("mover", stars_delta_1d=340, velocity=3.1)],
        stack_changes=[item("stack", changes=[{"path": "pyproject.toml", "added": ["fastmcp"], "removed": ["mcp"]}])],
        more=4,
    )
    assert render_digest(d) == [
        "<b>repka</b> · morning digest · 2026-11-07",
        "",
        "<b>MCP servers &amp; tooling</b>",
        '🆕 <a href="https://github.com/o/n">o/n</a> ★1.2k — d <i>tool · lvl 3</i>',
        '🔎 <a href="https://github.com/o/f">o/f</a> ★8.4k — d <i>library · lvl 4 · since 2023</i>',
        '🚀 <a href="https://github.com/o/r">o/r</a> <code>v1.2.0</code>',
        '📈 <a href="https://github.com/o/r">o/r</a> +340★/24h · velocity 3.1',
        '🧩 <a href="https://github.com/o/r">o/r</a> pyproject.toml +fastmcp −mcp',
        "…and 4 more",
    ]


def test_long_description_truncated_before_escaping():
    d = digest(new_repos=[item("new", desc="&" * 500, stars=1)])
    line = render_digest(d)[3]
    assert line.count("&amp;") == 139 and "…" in line and "&amp;…" in line


def test_empty_digest_renders_nothing():
    assert render_digest(Digest("08:30", "2026-11-07", [], [], [])) == []


def test_split_never_exceeds_limit_or_breaks_lines():
    lines = ["header", ""] + [f"line {i} " + "x" * 40 for i in range(200)]
    parts = split_messages(lines, limit=500)
    assert len(parts) > 1
    assert all(0 < len(p) <= 500 for p in parts)
    assert not any(p.startswith("\n") for p in parts)
    assert [ln for ln in "\n".join(parts).split("\n") if ln] == [ln for ln in lines if ln]  # nothing lost, no line cut
    assert split_messages(["a", "b"]) == ["a\nb"]


def test_slot_label():
    assert slot_label("08:30") == "morning"
    assert slot_label("11:59") == "morning"
    assert slot_label("12:00") == "evening"
    assert slot_label("19:30") == "evening"
