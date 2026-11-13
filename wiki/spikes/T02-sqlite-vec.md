# Spike T02: sqlite-vec under uv Python

[← Wiki home](../README.md) · [ADR-002](../spec/decisions.md#adr-002-sqlite--sqlite-vec-with-numpy-fallback)

## Verdict

**NO-GO for the `vec0` index. Use numpy brute-force cosine (ADR-002 fallback) for the vector store.**

- The extension does load: `enable_load_extension` exists, `sqlite_vec.load()` succeeds, and KNN returns the correct nearest rowid. So the pinned library works under this interpreter.
- Measured on an Apple Silicon Mac, `vec0` is slower than numpy at every size tested (table below), and it returns the same top-10 as numpy. Its only advantage is that it avoids loading the matrix into memory, which the numpy path does once at startup in about 0.1 s for 50,000 vectors. At Phase 1 scale (a few thousand repos, 768-dim) the index buys nothing.
- Revisit only if the embedding count grows past roughly 10^6 or the query path stops being brute-force friendly. The test (`tests/test_sqlite_vec_available.py`) keeps the vec0 path verified so the switch is cheap. The throwaway benchmark script used for the numbers below has been removed.

## Environment

| Item | Value |
|---|---|
| Machine | Apple Silicon (`aarch64-apple-darwin`), Darwin 27.0 |
| Python | 3.12.9 (uv-managed venv, `.venv/bin/python`); `enable_load_extension` available |
| SQLite library | 3.47.1 |
| sqlite-vec | 0.1.9 (pinned `==0.1.9` in `pyproject.toml`; latest on PyPI when pinned, 2026-11-07) |
| numpy | 2.5.4 |
| Benchmark | throwaway script (since removed), defaults: n=5000, dim=768, 100 queries, k=10 |

## Test

`uv run pytest tests/test_sqlite_vec_available.py -v` → **1 passed** (`test_sqlite_vec_knn_roundtrip`). It skips with a reason naming the Python build if extension loading is unavailable or `sqlite_vec.load` fails.

## Timings

Both paths rank by cosine similarity, and both return the same top-10 IDs (overlap 1.000). The vec0 timing includes the MATCH query only. The numpy timing covers the matrix-normalised dot product and top-k selection, with the matrix already in memory.

| Vectors | dim | vec0 mean / p95 per query | numpy mean / p95 per query | numpy matrix load (once) | Insert into vec0 |
|---|---|---|---|---|---|
| 5,000 | 768 | 3.41 / 3.47 ms | 0.12 / 0.15 ms | 6 ms | 0.18 s |
| 50,000 | 768 | 33.1 / 34.4 ms | 1.71 / 1.82 ms | 99 ms | not measured |

Ratio at these sizes: vec0 is about 28x slower at 5k and 19x slower at 50k.

Note on the first run: the spike initially used vec0's default L2 metric against a numpy cosine ranking. The top-10 overlap was then 0.35, which was a metric mismatch and not an index fault. The table above uses `distance_metric=cosine` on the vec0 table, which matches the numpy ranking exactly.

## Recommendation for the vector store

- Store vectors as float32 BLOBs in `embeddings` (the source of truth, unchanged).
- Implement `store/vectors.py` as numpy brute-force cosine only. Do not create `vec0` tables by default.
- Keep `sqlite-vec==0.1.9` pinned in `pyproject.toml` as an optional path, and keep `tests/test_sqlite_vec_available.py` green so the extension stays verified on this interpreter.
- Each vector row still carries `model` and `dim` (ADR-002).

## Status of linked documents

- [ADR-002](../spec/decisions.md#adr-002-sqlite--sqlite-vec-with-numpy-fallback): status line added, pointing here.
