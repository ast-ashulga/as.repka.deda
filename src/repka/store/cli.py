"""`repka db migrate` and `repka db stats`."""

from __future__ import annotations

import argparse

from repka.config import ConfigError, db_path
from repka.store import repo
from repka.store.db import connect, migrate


def _migrate(_args: argparse.Namespace) -> int:
    conn = connect()
    try:
        version = migrate(conn)
    finally:
        conn.close()
    print(f"schema version {version} at {db_path()}")
    return 0


def _stats(_args: argparse.Namespace) -> int:
    path = db_path()
    if not path.exists():
        raise ConfigError(f"no database at {path}; run `repka db migrate` first")
    conn = connect(readonly=True)
    try:
        counts = repo.table_counts(conn)
    finally:
        conn.close()
    for name, count in counts.items():
        print(f"{name} {count}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    db = sub.add_parser("db", help="SQLite store: migrate and inspect .state/repka.db")
    db_sub = db.add_subparsers(dest="db_command", metavar="SUBCOMMAND")
    db_sub.required = True

    migrate_cmd = db_sub.add_parser("migrate", help="create or upgrade the database schema")
    migrate_cmd.set_defaults(func=_migrate)

    stats_cmd = db_sub.add_parser("stats", help="print the row count of every table")
    stats_cmd.set_defaults(func=_stats)
