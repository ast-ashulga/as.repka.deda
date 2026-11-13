"""Landscape map: determinism, cache hit/recompute, one model per projection.

Needs umap-learn, scikit-learn and pandas: run with `uv run --group dashboard pytest`.
"""

import json
from collections import Counter

import numpy as np
import pytest

pytest.importorskip("pandas")
pytest.importorskip("umap")
pytest.importorskip("sklearn")

from helpers_digest import add_repo, add_verdict, make_db  # noqa: E402

from repka import dashboard_data as data  # noqa: E402
from repka import landscape  # noqa: E402
from repka.store.vectors import upsert_embedding  # noqa: E402

NOW = "2026-11-07T08:00:00Z"


def _clustered(per_cluster=30, dim=16, clusters=3, seed=0):
    """Gaussian blobs around well-separated unit centres; returns (matrix, true labels)."""
    rng = np.random.default_rng(seed)
    centres = np.eye(dim)[:clusters] * 5
    matrix = np.vstack([c + rng.normal(0, 0.3, (per_cluster, dim)) for c in centres]).astype(np.float32)
    return matrix, np.repeat(np.arange(clusters), per_cluster)


def test_projection_and_clusters_are_deterministic_and_recover_synthetic_clusters():
    matrix, truth = _clustered()
    points = landscape.project(matrix, "umap", seed=7)
    assert np.array_equal(points, landscape.project(matrix, "umap", seed=7))
    labels = landscape.cluster(points, min_cluster_size=5)
    assert np.array_equal(labels, landscape.cluster(points, min_cluster_size=5))
    for cluster_id in set(truth):
        found = Counter(labels[truth == cluster_id])
        top_label, top_count = found.most_common(1)[0]
        assert top_label != landscape.NOISE
        assert top_count >= 0.9 * (truth == cluster_id).sum()


def test_label_clusters_names_clusters_by_their_own_terms():
    import pandas as pd

    frame = pd.DataFrame(
        {
            "cluster": [0, 0, 1, 1, -1],
            "topics": [["mcp"], ["mcp"], ["rag"], ["rag"], []],
            "description": ["an mcp server", "mcp gateway", "rag pipeline", "rag toolkit", "misc"],
        }
    )
    terms = landscape.label_clusters(frame)
    assert terms[0][0] == "mcp" and terms[1][0] == "rag" and -1 not in terms


def test_label_clusters_survives_a_null_description():
    import pandas as pd

    frame = pd.DataFrame({"cluster": [0, 0], "topics": [["mcp"], ["mcp"]], "description": ["an mcp server", None]})
    assert landscape.label_clusters(frame)[0][0] == "mcp"


@pytest.fixture
def conn(tmp_path):
    conn = make_db(tmp_path / "repka.db")
    matrix, _ = _clustered(per_cluster=10, dim=8, clusters=2)
    for i, vec in enumerate(matrix, start=1):
        add_repo(conn, i, stars=10 * i)
        add_verdict(conn, i, accepted=True)
        upsert_embedding(conn, i, "m1", vec, f"h{i}", NOW)
    conn.commit()
    yield conn
    conn.close()


def _compute(conn, **kwargs):
    row = data.compute_landscape(conn, "d1", "m1", **kwargs)
    data.store_landscape(conn, row)
    return row


def test_cache_is_used_until_vectors_change(conn):
    assert data.landscape_frame(conn, "d1", "m1") == (None, "not_computed")
    first = _compute(conn)
    frame, status = data.landscape_frame(conn, "d1", "m1")
    assert status == "fresh" and len(frame) == 20
    assert {"x", "y", "cluster", "cluster_name", "kind", "stars"} <= set(frame.columns)

    new_vec = np.ones(8, dtype=np.float32)
    upsert_embedding(conn, 1, "m1", new_vec, "h1b", NOW)
    conn.commit()
    stale_frame, status = data.landscape_frame(conn, "d1", "m1")
    assert status == "stale" and len(stale_frame) == 20  # old points stay visible until Recompute

    second = _compute(conn)
    assert second["params_hash"] != first["params_hash"]
    assert data.landscape_frame(conn, "d1", "m1")[1] == "fresh"
    assert conn.execute("SELECT COUNT(*) FROM landscape_cache").fetchone()[0] == 1  # upsert, one row


def test_no_embeddings_state(conn):
    assert data.landscape_frame(conn, "d1", "other-model") == (None, "no_embeddings")
    assert data.compute_landscape(conn, "d1", "other-model") is None


def test_only_one_models_vectors_are_projected(conn):
    for i in range(1, 6):  # a second model: different dim, only some repos
        upsert_embedding(conn, i, "m2", [float(i), 1.0, 0.0], f"g{i}", NOW)
    conn.commit()
    ids_m1 = json.loads(data.compute_landscape(conn, "d1", "m1")["payload"])["repo_ids"]
    ids_m2 = json.loads(data.compute_landscape(conn, "d1", "m2")["payload"])["repo_ids"]
    assert ids_m1 == list(range(1, 21)) and ids_m2 == [1, 2, 3, 4, 5]


def test_a_locked_database_is_not_reported_as_not_computed(conn, monkeypatch):
    import sqlite3

    class Locked:
        """A connection whose landscape_cache read fails like a busy database."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args):
            if "landscape_cache" in sql:
                raise sqlite3.OperationalError("database is locked")
            return self._inner.execute(sql, *args)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        data.landscape_frame(Locked(conn), "d1", "m1")
    conn.execute("DROP TABLE landscape_cache")  # an unmigrated database still answers "not_computed"
    assert data.landscape_frame(conn, "d1", "m1") == (None, "not_computed")
