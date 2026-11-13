"""Small helpers shared by the command modules."""

from __future__ import annotations

import argparse
import sys


def positive_int(value: str) -> int:
    """argparse `type=` for a count that must be at least 1."""
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return n


def stderr_progress(message: str) -> None:
    """A plain progress line on stderr (stdout carries results)."""
    print(message, file=sys.stderr, flush=True)
