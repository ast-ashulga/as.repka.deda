"""The dashboard opens the DB read-only: a reader must see committed rows while a writer holds WAL."""

import sqlite3

import pytest

from repka.store.db import connect, migrate


def test_readonly_reader_sees_committed_rows_while_writer_holds_wal_write(tmp_path):
    path = tmp_path / "repka.db"
    writer = connect(path)
    migrate(writer)
    writer.execute("INSERT INTO domains (id) VALUES ('a')")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO domains (id) VALUES ('b')")  # uncommitted write lock

    reader = connect(path, readonly=True)
    try:
        assert [r["id"] for r in reader.execute("SELECT id FROM domains ORDER BY id")] == ["a"]
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO domains (id) VALUES ('c')")
    finally:
        reader.close()
        writer.commit()
        writer.close()
