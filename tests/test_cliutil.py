import argparse

import pytest

from repka.cliutil import positive_int, stderr_progress
from repka.store.db import iso, iso_or_none, parse_iso


def test_positive_int():
    assert positive_int("3") == 3
    for bad in ("0", "-1", "x", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            positive_int(bad)


def test_stderr_progress_writes_stderr_only(capsys):
    stderr_progress("hello")
    cap = capsys.readouterr()
    assert cap.err == "hello\n" and cap.out == ""


def test_iso_round_trip_and_none():
    value = parse_iso("2026-11-07T08:30:00Z")
    assert iso(value) == "2026-11-07T08:30:00Z" and iso_or_none(value) == "2026-11-07T08:30:00Z"
    assert iso_or_none(None) is None and parse_iso(None) is None
