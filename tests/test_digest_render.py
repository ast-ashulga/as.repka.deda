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


def test_hostile_stack_text_never_exceeds_message_limit():
    changes = [{"path": "&" * 10_000, "added": ["<" * 10_000] * 20, "removed": ["&" * 10_000]} for _ in range(100)]
    d = digest(stack_changes=[item("stack", name="o/" + "&" * 5000, changes=changes)])
    parts = split_messages(render_digest(d))
    assert parts and all(len(p) <= 4096 for p in parts)
    assert not re.search(r"&[^;&\s]{0,6}$", parts[-1]) or parts[-1].endswith("…")


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


# --- insights format -------------------------------------------------------------------------------


def idigest(*, more_by_kind=None, **sections) -> Digest:
    section = DigestSection("d1", "MCP", **sections)
    if more_by_kind:
        section.more_by_kind = dict(more_by_kind)
        section.more = sum(more_by_kind.values())
    return Digest("08:30", "2026-11-07", [section], [], [], insights=True)


def test_insights_item_is_three_lines_with_tags():
    d = idigest(
        new_repos=[
            item("new", "o/n", "A tool", stars=1200, kind="framework", level=3, facets=[("self_modifies", 0.94)])
        ]
    )
    lines = render_digest(d)
    assert lines[3] == (
        '🆕 <a href="https://github.com/o/n">o/n</a> ★1.2k\n'
        "<i>A tool</i>\n"
        "<code>framework · lvl 3</code> · self modifies"
    )
    assert lines[3].split("\n")[2] == "<code>framework · lvl 3</code> · self modifies"


def test_insights_found_and_mover_items():
    d = idigest(
        new_repos=[item("found", "o/f", "Old", stars=8400, kind="library", level=4, since=2023, facets=[])],
        movers=[item("mover", "o/m", "Hot", stars=5000, stars_delta_1d=340, kind="tool", level=2, facets=[("a_b", 0.8), ("c", 0.7)])],
    )
    lines = render_digest(d)
    assert lines[3] == (
        '🔎 <a href="https://github.com/o/f">o/f</a> ★8.4k\n<i>Old</i>\n<code>library · lvl 4</code>'
    )
    assert lines[4] == (
        '📈 <a href="https://github.com/o/m">o/m</a> ★5k <i>(+340★/24h)</i>\n'
        "<i>Hot</i>\n"
        "<code>tool · lvl 2</code> · a b · c"
    )


def test_insights_item_stays_in_one_message():
    repos = [
        item("new", f"o/r{i}", f"description {i} " + "x" * 120, stars=i, kind="tool", level=3, facets=[("self_modifies", 0.9)])
        for i in range(40)
    ]
    lines = render_digest(idigest(new_repos=repos))
    item_elements = [ln for ln in lines if "\n" in ln and ln.startswith("🆕")]
    assert len(item_elements) == 40
    messages = split_messages(lines)
    assert len(messages) >= 2  # otherwise the check below proves nothing
    assert all(len(m) <= 4096 for m in messages)
    for element in item_elements:
        assert sum(element in m for m in messages) == 1  # whole, in exactly one message


def test_insights_escapes_untrusted_text():
    d = idigest(
        new_repos=[
            item(
                "new", "o/<b>x", "<b>&\x07x", stars=1, kind="k<i>\x07", level=1,
                facets=[("a<b", 0.9), ("\x07q&", 0.9)],
            )
        ],
        movers=[item("mover", "o/m", "<script>", stars=1, stars_delta_1d=5, kind="t", level=1, facets=[("<u>", 0.8)])],
    )
    text = "\n".join(render_digest(d))
    assert "\x07" not in text
    assert "&lt;b&gt;&amp;x" in text
    assert "a&lt;b" in text and "q&amp;" in text and "&lt;u&gt;" in text and "&lt;script&gt;" in text
    assert "<script>" not in text and "<u>" not in text and "a<b" not in text
    assert set(re.findall(r"</?(\w+)", text)) <= {"b", "i", "a", "code"}
    assert "k&lt;i&gt;" in text  # escaped kind
    assert "o/&lt;b&gt;x" in text  # escaped repo name


def test_insights_legend_lists_only_present_icons():
    d = idigest(new_repos=[item("new", stars=1, facets=[])], movers=[item("mover", stars=1, stars_delta_1d=5, facets=[])])
    lines = render_digest(d)
    legend = lines[-1]
    assert legend == "🆕 new repo · 📈 star spike · tags = decider answers (p ≥ 0.70)"
    assert lines[-2] == ""
    assert "🔎" not in legend and "🧩" not in legend and "🚀" not in legend


def test_insights_legend_with_releases_and_stack_and_configured_threshold():
    d = idigest(
        new_repos=[item("found", stars=1, facets=[])],
        releases=[item("release", tag="v1")],
        stack_changes=[item("stack", changes=[{"path": "p", "added": ["a"]}])],
    )
    d.facet_min_p = 0.85
    lines = render_digest(d)
    assert lines[-1] == "🔎 newly found · 🧩 stack change · 🚀 release · tags = decider answers (p ≥ 0.85)"
    assert '🚀 <a href="https://github.com/o/r">o/r</a> <code>v1</code>' in lines  # release line unchanged


def test_insights_more_counter_by_kind():
    d = idigest(new_repos=[item("new", stars=1, facets=[])], more_by_kind={"new": 5, "found": 7})
    assert render_digest(d)[-1] == "…and 12 more: 5 🆕, 7 🔎 (repka digest --more)"
    assert not any(ln.startswith("…and") and "more:" not in ln for ln in render_digest(d))  # no per-section line


def test_insights_more_counter_sums_sections():
    a = DigestSection("d1", "A", new_repos=[item("new", stars=1, facets=[])], more=2, more_by_kind={"new": 2})
    b = DigestSection("d2", "B", new_repos=[item("new", stars=1, facets=[])], more=4, more_by_kind={"new": 1, "mover": 3})
    d = Digest("08:30", "2026-11-07", [a, b], [], [], insights=True)
    assert render_digest(d)[-1] == "…and 6 more: 3 🆕, 3 📈 (repka digest --more)"


def test_insights_no_counter_without_more():
    d = idigest(new_repos=[item("new", stars=1, facets=[])])
    assert not any("more" in ln for ln in render_digest(d))


def test_render_unchanged_when_insights_off():
    # facets and more_by_kind present in the data must not alter the flags-off format
    section = DigestSection(
        "d1", "MCP",
        new_repos=[item("new", "o/n", "d", stars=1200, kind="tool", level=3, facets=[("self_modifies", 0.94)])],
        movers=[item("mover", "o/m", "d", stars=5, stars_delta_1d=340, velocity=3.1, kind="tool", level=1, facets=[])],
        more=4, more_by_kind={"new": 4},
    )
    d = Digest("08:30", "2026-11-07", [section], [], [])
    assert render_digest(d) == [
        "<b>repka</b> · morning digest · 2026-11-07",
        "",
        "<b>MCP</b>",
        '🆕 <a href="https://github.com/o/n">o/n</a> ★1.2k — d <i>tool · lvl 3</i>',
        '📈 <a href="https://github.com/o/m">o/m</a> +340★/24h · velocity 3.1',
        "…and 4 more",
    ]
    d.insights = True
    assert render_digest(d) != render_digest(Digest("08:30", "2026-11-07", [section], [], []))


def test_insights_releases_render_last_in_section():
    d = idigest(
        new_repos=[item("new", "o/n", stars=1, facets=[])],
        releases=[item("release", "o/rel", tag="v1")],
        movers=[item("mover", "o/m", stars=1, stars_delta_1d=5, facets=[])],
        stack_changes=[item("stack", "o/s", changes=[{"path": "p", "added": ["a"]}])],
    )
    lines = render_digest(d)
    pos = {k: next(i for i, ln in enumerate(lines) if f"o/{k}</a>" in ln) for k in ("n", "rel", "m", "s")}
    assert pos["n"] < pos["m"] < pos["s"] < pos["rel"]


def test_insights_legend_explains_counter_only_icons():
    d = idigest(new_repos=[item("new", stars=1, facets=[])], more_by_kind={"found": 3, "stack": 0})
    lines = render_digest(d)
    assert lines[-2] == "🆕 new repo · 🔎 newly found · tags = decider answers (p ≥ 0.70)"
    assert lines[-1] == "…and 3 more: 3 🔎 (repka digest --more)"
