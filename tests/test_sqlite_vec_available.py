"""Does sqlite-vec load and answer KNN under this uv Python build?

Skips (with the reason) when the interpreter cannot load SQLite extensions;
repka does not use a vec0 index (vectors are stored as BLOBs and compared with numpy), so this is informational.
"""

import sqlite3
import sys

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec")


def _python_build() -> str:
    return f"{sys.version.split()[0]} ({sys.executable})"


def test_sqlite_vec_knn_roundtrip():
    conn = sqlite3.connect(":memory:")
    if not hasattr(conn, "enable_load_extension"):
        pytest.skip(f"extension loading unsupported: {_python_build()}")
    try:
        conn.enable_load_extension(True)
    except (sqlite3.OperationalError, AttributeError) as exc:
        pytest.skip(f"extension loading disabled ({exc}): {_python_build()}")
    try:
        sqlite_vec.load(conn)
    except (sqlite3.OperationalError, AttributeError) as exc:
        pytest.skip(f"sqlite-vec could not load ({exc}): {_python_build()}")
    finally:
        conn.enable_load_extension(False)

    conn.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[4])")
    vectors = {
        1: [1.0, 0.0, 0.0, 0.0],
        2: [0.0, 1.0, 0.0, 0.0],
        3: [0.0, 0.0, 1.0, 0.0],
    }
    for rowid, vec in vectors.items():
        conn.execute(
            "INSERT INTO v(rowid, embedding) VALUES (?, ?)",
            (rowid, sqlite_vec.serialize_float32(vec)),
        )
    rows = conn.execute(
        "SELECT rowid FROM v WHERE embedding MATCH ? AND k = 3 ORDER BY distance",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    ).fetchall()
    assert rows[0][0] == 1
    assert len(rows) == 3
    conn.close()
