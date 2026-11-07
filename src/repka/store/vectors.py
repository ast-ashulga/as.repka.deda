"""Embedding vectors: float32 BLOBs in `embeddings` (source of truth) and numpy brute-force KNN.

No vec0 index: P1-T02 measured sqlite-vec as 19-28x slower than numpy at 5k-50k vectors
(wiki/spikes/T02-sqlite-vec.md). Vectors of different models are never mixed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import Protocol

import numpy as np

_F32 = np.dtype("<f4")


def to_blob(vec: Sequence[float]) -> bytes:
    return np.asarray(vec, dtype=_F32).tobytes()


def from_blob(b: bytes, dim: int) -> np.ndarray:
    if len(b) != dim * _F32.itemsize:
        raise ValueError(f"vector blob is {len(b)} bytes, expected {dim * _F32.itemsize} for dim {dim}")
    return np.frombuffer(b, dtype=_F32).astype(np.float32)


def upsert_embedding(
    conn: sqlite3.Connection, repo_id: int, model: str, vector: Sequence[float], input_hash: str, now: str
) -> None:
    dim = len(vector)
    if dim == 0:
        raise ValueError("empty embedding vector")
    other = conn.execute(
        "SELECT dim FROM embeddings WHERE model = ? AND dim != ? LIMIT 1", (model, dim)
    ).fetchone()
    if other is not None:
        raise ValueError(f"model {model!r} already has {other['dim']}-dim vectors, got {dim}")
    conn.execute(
        """INSERT INTO embeddings (repo_id, model, dim, input_hash, vector, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (repo_id, model) DO UPDATE SET
               dim = excluded.dim, input_hash = excluded.input_hash,
               vector = excluded.vector, created_at = excluded.created_at""",
        (repo_id, model, dim, input_hash, to_blob(vector), now),
    )


def embedding_hash(conn: sqlite3.Connection, repo_id: int, model: str) -> str | None:
    row = conn.execute(
        "SELECT input_hash FROM embeddings WHERE repo_id = ? AND model = ?", (repo_id, model)
    ).fetchone()
    return None if row is None else row["input_hash"]


def load_matrix(
    conn: sqlite3.Connection, model: str, repo_ids: Iterable[int] | None = None
) -> tuple[list[int], np.ndarray]:
    """Repo ids (ascending) and their L2-normalized (n, dim) float32 matrix for one model."""
    empty = ([], np.zeros((0, 0), dtype=np.float32))
    sql = "SELECT repo_id, dim, vector FROM embeddings WHERE model = ?"
    params: list[int | str] = [model]
    if repo_ids is not None:
        ids = sorted(set(repo_ids))
        if not ids:
            return empty
        sql += f" AND repo_id IN ({','.join('?' * len(ids))})"
        params.extend(ids)
    rows = conn.execute(sql + " ORDER BY repo_id", params).fetchall()
    if not rows:
        return empty
    dim = rows[0]["dim"]
    ids = [row["repo_id"] for row in rows]
    matrix = np.frombuffer(b"".join(row["vector"] for row in rows), dtype=_F32)
    matrix = matrix.reshape(len(rows), dim).astype(np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, matrix / norms


class VectorIndex(Protocol):
    def knn(
        self, query: Sequence[float], k: int, candidates: set[int] | None = None
    ) -> list[tuple[int, float]]:
        """(repo_id, cosine similarity) sorted by similarity descending."""
        ...


class NumpyIndex:
    """Brute-force cosine KNN over the model's vectors, loaded once at construction."""

    def __init__(self, conn: sqlite3.Connection, model: str) -> None:
        self.model = model
        self._ids, self._matrix = load_matrix(conn, model)
        self._ids_arr = np.asarray(self._ids, dtype=np.int64)

    def knn(
        self, query: Sequence[float], k: int, candidates: set[int] | None = None
    ) -> list[tuple[int, float]]:
        if k <= 0 or not self._ids:
            return []
        q = np.asarray(query, dtype=np.float32)
        dim = self._matrix.shape[1]
        if q.shape != (dim,):
            raise ValueError(f"query has dim {q.size}, index for {self.model!r} has {dim}")
        norm = np.linalg.norm(q)
        if norm:
            q = q / norm
        sims = self._matrix @ q
        positions = np.arange(len(self._ids))
        if candidates is not None:
            mask = np.isin(self._ids_arr, np.fromiter(candidates, dtype=np.int64))
            positions = positions[mask]
        if positions.size == 0:
            return []
        order = np.argsort(-sims[positions], kind="stable")[:k]
        return [(self._ids[i], float(sims[i])) for i in positions[order]]


def get_index(conn: sqlite3.Connection, model: str) -> VectorIndex:
    return NumpyIndex(conn, model)
