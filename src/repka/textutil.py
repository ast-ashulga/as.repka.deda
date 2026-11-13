"""Helpers for text that comes from other people (repo names, READMEs, release notes)."""

from __future__ import annotations

import re

# C0 and C1 control characters except tab and newline. ESC starts terminal escape sequences
# (titles, cursor moves, clipboard writes), so none of them may reach a terminal.
_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """Drop terminal control characters; newlines and tabs are kept."""
    return _UNSAFE.sub("", text)


_FENCE = re.compile(r'"{3,}')


def neutralize_fence(text: str) -> str:
    """Break up runs of three or more double quotes with zero-width spaces, so untrusted text placed inside a
    triple-quoted block of a model prompt cannot close that block. Shorter runs are left alone."""
    return _FENCE.sub(lambda m: "\u200b".join(m.group()), text)


def html_escape(text: str) -> str:
    """Escape & < > and " for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def truncate(text: str, n: int) -> str:
    """At most n characters; a cut text ends with an ellipsis."""
    if len(text) <= n:
        return text
    return text[: max(n - 1, 0)] + "…"
