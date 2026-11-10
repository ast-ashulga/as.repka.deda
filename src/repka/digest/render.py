"""Digest rendering: Telegram HTML (tags b, i, a, code only), split into messages of <= 4096 chars.

Every piece of repo text is untrusted: control characters are dropped, then it is
truncated, then HTML-escaped (in that order, so an entity is never cut in half).
"""

from __future__ import annotations

import re
from typing import Any

from repka.digest.build import Digest
from repka.models import DigestItem
from repka.textutil import html_escape, printable, truncate

DESC_CHARS = 140
MAX_DEPS_SHOWN = 5
DEP_CHARS = 60
PATH_CHARS = 80
MAX_CHANGES_SHOWN = 3
TAG_CHARS = 40  # insights: kind and facet labels

# Insights legend and overflow counter, in display order: line_kind -> (icon, legend text).
_LEGEND = {
    "new": ("🆕", "new repo"),
    "found": ("🔎", "newly found"),
    "mover": ("📈", "star spike"),
    "stack": ("🧩", "stack change"),
    "release": ("🚀", "release"),
}


def slot_label(slot: str) -> str:
    return "morning" if int(slot.split(":")[0]) < 12 else "evening"


def _text(value: Any, limit: int | None = None) -> str:
    s = " ".join(printable(str(value)).split())
    return html_escape(truncate(s, limit) if limit else s)


def _stars(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 999_950:
        return f"{n / 1000:.1f}".removesuffix(".0") + "k"
    return f"{n / 1_000_000:.1f}".removesuffix(".0") + "M"


def _link(item: DigestItem) -> str:
    return f'<a href="{html_escape(printable(item.html_url))}">{_text(item.full_name)}</a>'


def _names(sign: str, names: list[Any]) -> str:
    shown = [f"{sign}{_text(n, DEP_CHARS)}" for n in names[:MAX_DEPS_SHOWN]]
    if len(names) > MAX_DEPS_SHOWN:
        shown.append(f"{sign}…{len(names) - MAX_DEPS_SHOWN}")
    return " ".join(shown)


def _repo_line(item: DigestItem) -> str:
    f = item.text_fields
    icon = "🆕" if item.line_kind == "new" else "🔎"
    line = f"{icon} {_link(item)} ★{_stars(int(f.get('stars') or 0))}"
    if f.get("description"):
        line += f" — {_text(f['description'], DESC_CHARS)}"
    tags = []
    if f.get("kind"):
        tags.append(_text(f["kind"]))
    if f.get("level") is not None:
        tags.append(f"lvl {int(f['level'])}")
    if item.line_kind == "found" and f.get("since"):
        tags.append(f"since {int(f['since'])}")
    if tags:
        line += f" <i>{' · '.join(tags)}</i>"
    return line


def _release_line(item: DigestItem) -> str:
    line = f"🚀 {_link(item)}"
    if item.text_fields.get("tag"):
        line += f" <code>{_text(item.text_fields['tag'])}</code>"
    return line


def _mover_line(item: DigestItem) -> str:
    f = item.text_fields
    line = f"📈 {_link(item)} +{int(f['stars_delta_1d'])}★/24h"
    if f.get("velocity") is not None:
        line += f" · velocity {float(f['velocity']):g}"
    return line


def _stack_line(item: DigestItem) -> str:
    parts = []
    changes = item.text_fields.get("changes", [])
    for c in changes[:MAX_CHANGES_SHOWN]:
        bits = [_text(c.get("path") or "", PATH_CHARS)]
        for sign, names in (("+", c.get("added") or []), ("−", c.get("removed") or [])):
            if names:
                bits.append(_names(sign, names))
        parts.append(" ".join(b for b in bits if b))
    if len(changes) > MAX_CHANGES_SHOWN:
        parts.append(f"…{len(changes) - MAX_CHANGES_SHOWN} more files")
    return f"🧩 {_link(item)} {'; '.join(parts)}"


def _insight_item(item: DigestItem) -> str:
    """A 🆕/🔎/📈 item as ONE list element of up to three lines (split_messages never cuts an element)."""
    f = item.text_fields
    icon = {"new": "🆕", "found": "🔎", "mover": "📈"}[item.line_kind]
    head = f"{icon} {_link(item)} ★{_stars(int(f.get('stars') or 0))}"
    if item.line_kind == "mover":
        head += f" <i>(+{int(f['stars_delta_1d'])}★/24h)</i>"
    out = [head]
    if f.get("description"):
        out.append(f"<i>{_text(f['description'], DESC_CHARS)}</i>")
    meta = []
    if f.get("kind"):
        meta.append(_text(f["kind"], TAG_CHARS))
    if f.get("level") is not None:
        meta.append(f"lvl {int(f['level'])}")
    third = f"<code>{' · '.join(meta)}</code>" if meta else ""
    for facet_id, _p in f.get("facets") or []:
        label = _text(str(facet_id).replace("_", " "), TAG_CHARS)
        if label:
            third += f" · {label}" if third else label
    if third:
        out.append(third)
    return "\n".join(out)


def _insights_footer(d: Digest) -> list[str]:
    present = {i.line_kind for s in d.sections for i in (*s.new_repos, *s.releases, *s.movers, *s.stack_changes)}
    legend = [f"{icon} {text}" for kind, (icon, text) in _LEGEND.items() if kind in present]
    legend.append(f"tags = decider answers (p ≥ {d.facet_min_p:.2f})")
    out = ["", " · ".join(legend)]
    by_kind: dict[str, int] = {}
    total = 0
    for s in d.sections:
        total += sum(s.more_by_kind.values()) or s.more
        for kind, n in s.more_by_kind.items():
            by_kind[kind] = by_kind.get(kind, 0) + n
    if total:
        split = ", ".join(f"{by_kind[k]} {_LEGEND[k][0]}" for k in _LEGEND if by_kind.get(k))
        out.append(f"…and {total} more{': ' + split if split else ''} (repka digest --more)")
    return out


def render_digest(d: Digest) -> list[str]:
    """Digest lines; empty when there is nothing to show."""
    if d.item_count == 0:
        return []
    lines = [f"<b>repka</b> · {slot_label(d.slot)} digest · {_text(d.local_date)}"]
    for s in d.sections:
        lines += ["", f"<b>{_text(s.domain_name)}</b>"]
        if d.insights:
            lines += [_insight_item(i) for i in s.new_repos]
            lines += [_release_line(i) for i in s.releases]
            lines += [_insight_item(i) for i in s.movers]
            lines += [_stack_line(i) for i in s.stack_changes]
            continue
        lines += [_repo_line(i) for i in s.new_repos]
        lines += [_release_line(i) for i in s.releases]
        lines += [_mover_line(i) for i in s.movers]
        lines += [_stack_line(i) for i in s.stack_changes]
        if s.more:
            lines.append(f"…and {s.more} more")
    if d.insights:
        lines += _insights_footer(d)
    return lines


def _squash(line: str, limit: int) -> str:
    plain = re.sub(r"<[^>]*>", "", line)[: max(limit - 1, 0)]
    plain = re.sub(r"&[^;&\s]{0,6}$", "", plain)  # never leave half an entity
    return plain + "…"


def split_messages(lines: list[str], limit: int = 4096) -> list[str]:
    """Pack lines into messages of at most `limit` characters.

    Rendered lines are bounded far below the limit; as a last resort a line longer than
    `limit` loses its tags (so none is left open) and is cut at `limit` with an ellipsis.
    """
    messages: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if len(line) > limit:
            line = _squash(line, limit)
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            messages.append("\n".join(current).strip("\n"))
            current, size, extra = [], 0, len(line)
        if not current and not line:  # no blank line at the start of a message
            continue
        current.append(line)
        size += extra
    if current:
        messages.append("\n".join(current).strip("\n"))
    return [m for m in messages if m]
