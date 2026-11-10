"""The renderer that currently owns stderr, so deep code (logging, the triage worker, CLI prints) can cooperate.

`live` redraws one line and `jsonl` promises that every stderr line is JSON; both need every other writer to go
through `stderr_line`. With no active renderer (or one that does not own stderr) it is a plain stderr print.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_active: Any = None  # a renderer with `owns_stderr` and `log_line(text, level)`, or None


@contextmanager
def activate(renderer: Any) -> Iterator[None]:
    global _active
    previous, _active = _active, renderer
    try:
        yield
    finally:
        _active = previous


def owns_stderr() -> bool:
    return bool(getattr(_active, "owns_stderr", False))


def stderr_line(text: str, level: str = "INFO") -> None:
    """One line for the human or agent watching stderr, rendered by whoever owns it."""
    if owns_stderr():
        try:
            _active.log_line(text, level)
            return
        except Exception:  # a broken renderer must not lose the message
            pass
    print(text, file=sys.stderr, flush=True)
