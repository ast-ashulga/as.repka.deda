"""Connections and schema migrations. Callers own transactions (see repo.py)."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from repka.config import db_path

_MIGRATION = re.compile(r"(\d{3})_.+\.sql")


def connect(path: Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    """Open the repka database (default: config.db_path()).

    Writable connections use WAL and create the parent directory. Read-only connections
    open with `mode=ro` and fail if the file does not exist.
    """
    target = Path(path) if path is not None else db_path()
    if readonly:
        conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _migrations() -> list[tuple[int, str]]:
    found = []
    for entry in resources.files("repka.store").joinpath("migrations").iterdir():
        match = _MIGRATION.fullmatch(entry.name)
        if match:
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    return sorted(found)


def _current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if exists is None:
        return 0
    return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration above the stored version, each in its own transaction.

    Each migration file inserts its own schema_version row, so the version bump commits
    atomically with the schema change. The write lock is taken first (BEGIN IMMEDIATE) and
    the version is re-read under it, so a concurrent migrator that got there first makes
    this step a no-op instead of a failure. Returns the new version.
    """
    conn.commit()
    current = _current_version(conn)
    previous = conn.autocommit
    conn.autocommit = True  # manual transactions: executescript must not release the lock
    try:
        for number, sql in _migrations():
            if number <= current:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                if _current_version(conn) < number:
                    conn.executescript(sql)
                conn.execute("COMMIT")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            current = number
    finally:
        conn.autocommit = previous
    return current


def utcnow() -> str:
    """Current time as UTC ISO-8601 with seconds, e.g. 2026-11-07T08:30:00Z."""
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
