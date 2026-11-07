import numpy as np
import pytest

from repka.store import vectors
from repka.store.db import connect, migrate

NOW = "2026-11-07T08:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "vectors.db")
    migrate(c)
    for repo_id in range(1, 4):
        c.execute(
            "INSERT INTO repos (repo_id, node_id, full_name, html_url, first_seen_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (repo_id, f"R_{repo_id}", f"o/r{repo_id}", f"https://github.com/o/r{repo_id}", NOW, NOW),
        )
    yield c
    c.close()


def test_blob_roundtrip():
    vec = [0.5, -1.25, 3.0]
    blob = vectors.to_blob(vec)
    assert len(blob) == 12
    assert np.allclose(vectors.from_blob(blob, 3), vec)


def test_dim_mismatch_for_same_model_raises(conn):
    vectors.upsert_embedding(conn, 1, "m", [1.0, 0.0, 0.0], "h1", NOW)
    with pytest.raises(ValueError):
        vectors.upsert_embedding(conn, 2, "m", [1.0, 0.0], "h2", NOW)


def test_upsert_replaces_and_reports_hash(conn):
    assert vectors.embedding_hash(conn, 1, "m") is None
    vectors.upsert_embedding(conn, 1, "m", [1.0, 0.0], "h1", NOW)
    vectors.upsert_embedding(conn, 1, "m", [0.0, 1.0], "h2", NOW)
    assert vectors.embedding_hash(conn, 1, "m") == "h2"
    ids, matrix = vectors.load_matrix(conn, "m")
    assert ids == [1]
    assert np.allclose(matrix, [[0.0, 1.0]])


def test_models_are_isolated(conn):
    vectors.upsert_embedding(conn, 1, "a", [1.0, 0.0, 0.0], "h", NOW)
    vectors.upsert_embedding(conn, 2, "b", [0.0, 1.0], "h", NOW)
    hits = vectors.get_index(conn, "a").knn([0.0, 1.0, 0.0], 5)
    assert [repo_id for repo_id, _ in hits] == [1]


def test_numpy_knn_orders_by_cosine(conn):
    vectors.upsert_embedding(conn, 1, "m", [10.0, 0.0], "h", NOW)  # same direction as [1, 0]
    vectors.upsert_embedding(conn, 2, "m", [1.0, 1.0], "h", NOW)
    vectors.upsert_embedding(conn, 3, "m", [0.0, 1.0], "h", NOW)
    hits = vectors.get_index(conn, "m").knn([1.0, 0.1], 3)
    assert [repo_id for repo_id, _ in hits] == [1, 2, 3]
    assert hits[0][1] > hits[1][1] > hits[2][1]
    assert hits[0][1] == pytest.approx(1.0, abs=0.01)


def test_candidates_filter_restricts_results(conn):
    vectors.upsert_embedding(conn, 1, "m", [1.0, 0.0], "h", NOW)
    vectors.upsert_embedding(conn, 2, "m", [0.9, 0.1], "h", NOW)
    vectors.upsert_embedding(conn, 3, "m", [0.0, 1.0], "h", NOW)
    hits = vectors.get_index(conn, "m").knn([1.0, 0.0], 5, candidates={2, 3})
    assert [repo_id for repo_id, _ in hits] == [2, 3]
