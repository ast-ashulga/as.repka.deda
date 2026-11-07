"""Digest rendering: Telegram HTML (tags b, i, a, code only), split into messages of <= 4096 chars.

Every piece of repo text is untrusted: control characters are dropped, then it is
truncated, then HTML-escaped (in that order, so an entity is never cut in half).
"""

from __future__ import annotations

from typing import Any

from repka.digest.build import Digest
from repka.models import DigestItem
from repka.textutil import html_escape, printable, truncate

DESC_CHARS = 140
MAX_DEPS_SHOWN = 5


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
    shown = [f"{sign}{_text(n)}" for n in names[:MAX_DEPS_SHOWN]]
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
    for c in item.text_fields.get("changes", []):
        bits = [_text(c.get("path") or "")]
        for sign, names in (("+", c.get("added") or []), ("−", c.get("removed") or [])):
            if names:
                bits.append(_names(sign, names))
        parts.append(" ".join(b for b in bits if b))
    return f"🧩 {_link(item)} {'; '.join(parts)}"


def render_digest(d: Digest) -> list[str]:
    """Digest lines; empty when there is nothing to show."""
    if d.item_count == 0:
        return []
    lines = [f"<b>repka</b> · {slot_label(d.slot)} digest · {_text(d.local_date)}"]
    for s in d.sections:
        lines += ["", f"<b>{_text(s.domain_name)}</b>"]
        lines += [_repo_line(i) for i in s.new_repos]
        lines += [_release_line(i) for i in s.releases]
        lines += [_mover_line(i) for i in s.movers]
        lines += [_stack_line(i) for i in s.stack_changes]
        if s.more:
            lines.append(f"…and {s.more} more")
    return lines


def split_messages(lines: list[str], limit: int = 4096) -> list[str]:
    """Pack lines into messages of at most `limit` characters; a line is never cut.

    (Every rendered line is far shorter than the limit by construction.)
    """
    messages: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
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
