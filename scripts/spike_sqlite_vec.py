"""Spike P1-T02: time sqlite-vec vec0 KNN against numpy cosine brute force.

Run: uv run python scripts/spike_sqlite_vec.py [--n 5000] [--dim 768] [--queries 100] [--k 10]
Output: environment, insert time, and per-query timings for both paths.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time

import numpy as np
import sqlite_vec


def _ms(seconds: float) -> float:
    return seconds * 1000.0


def run(n: int, dim: int, queries: int, k: int, seed: int = 0) -> dict[str, float | str | bool]:
    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((n, dim), dtype=np.float32)
    qvecs = rng.standard_normal((queries, dim), dtype=np.float32)

    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # cosine metric so both paths rank by the same measure (vec0 defaults to L2).
    conn.execute(f"CREATE VIRTUAL TABLE v USING vec0(embedding float[{dim}] distance_metric=cosine)")

    t0 = time.perf_counter()
    with conn:
        conn.executemany(
            "INSERT INTO v(rowid, embedding) VALUES (?, ?)",
            [(i + 1, sqlite_vec.serialize_float32(vectors[i].tolist())) for i in range(n)],
        )
    insert_s = time.perf_counter() - t0

    # numpy path: BLOBs in a table (the source of truth per ADR-002), loaded once.
    conn.execute("CREATE TABLE emb (id INTEGER PRIMARY KEY, vec BLOB NOT NULL)")
    with conn:
        conn.executemany(
            "INSERT INTO emb(id, vec) VALUES (?, ?)",
            [(i + 1, vectors[i].tobytes()) for i in range(n)],
        )
    t0 = time.perf_counter()
    rows = conn.execute("SELECT id, vec FROM emb ORDER BY id").fetchall()
    matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(n, dim)
    matrix_norm = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    load_s = time.perf_counter() - t0
    ids = np.array([r[0] for r in rows])

    vec_times: list[float] = []
    vec_top: list[list[int]] = []
    for q in qvecs:
        t0 = time.perf_counter()
        found = conn.execute(
            "SELECT rowid FROM v WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(q.tolist()), k),
        ).fetchall()
        vec_times.append(time.perf_counter() - t0)
        vec_top.append([r[0] for r in found])

    np_times: list[float] = []
    np_top: list[list[int]] = []
    for q in qvecs:
        t0 = time.perf_counter()
        qn = q / np.linalg.norm(q)
        sims = matrix_norm @ qn
        top = np.argpartition(-sims, k)[:k]
        top = top[np.argsort(-sims[top])]
        np_times.append(time.perf_counter() - t0)
        np_top.append(ids[top].tolist())

    overlap = float(
        np.mean([len(set(a) & set(b)) / k for a, b in zip(vec_top, np_top, strict=True)])
    )
    conn.close()
    return {
        "n": n,
        "dim": dim,
        "k": k,
        "queries": queries,
        "insert_s": insert_s,
        "numpy_load_s": load_s,
        "vec0_ms_mean": _ms(float(np.mean(vec_times))),
        "vec0_ms_p95": _ms(float(np.percentile(vec_times, 95))),
        "numpy_ms_mean": _ms(float(np.mean(np_times))),
        "numpy_ms_p95": _ms(float(np.percentile(np_times, 95))),
        "topk_overlap": overlap,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--n", type=int, default=5000)
    parser.add_argument("--dim", type=int, default=768)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--k", type=int, default=10)
    args = parser.parse_args(argv)

    print(f"python: {sys.version.split()[0]} {sys.executable}")
    print(f"sqlite3 library: {sqlite3.sqlite_version}")
    print(f"sqlite-vec: {sqlite_vec.__version__}")
    print(f"numpy: {np.__version__}")
    result = run(args.n, args.dim, args.queries, args.k)
    for key, value in result.items():
        if isinstance(value, float):
            print(f"{key}: {value:.3f}")
        else:
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
