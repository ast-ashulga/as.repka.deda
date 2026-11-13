# Research: Local data store and embeddings

[← Wiki home](../README.md) · Related: [architecture § data model](../spec/architecture.md#4-data-model-sqlite) · [ADR-002](../spec/decisions.md) · [spike T02](../spikes/T02-sqlite-vec.md)

Status: researched 2026-11-07.

## 1. Vector extension: sqlite-vss → sqlite-vec

- **sqlite-vss is not in active development**; its author moved to **sqlite-vec**, described as easier to install and use and written because vss was not optimized enough.
- sqlite-vec: `pip install sqlite-vec`, load with `sqlite_vec.load(conn)`; `vec0` virtual tables; KNN via `WHERE embedding MATCH ? ORDER BY distance LIMIT k`. **Pre-v1: expect breaking changes** → pin the exact version in `pyproject.toml`.
- **macOS gotcha:** loading an extension needs `sqlite3.Connection.enable_load_extension(True)`. Apple's system Python and some builds compile sqlite3 without extension loading. uv-managed CPython (python-build-standalone) is expected to support it **(verify in spike [P1-T02](../archive/plans/phase-1/M0-foundation.md#p1-t02-spike-sqlite-vec-loads-under-uv-python))**.
- **Fallback (decided in advance):** store vectors as `float32` BLOBs in a plain table and do brute-force cosine similarity in numpy. For a few thousand repos per domain this is milliseconds. The vector index is therefore an *optimization*, never the source of truth.

DuckDB (with the `vss` extension) was considered: good for analytics but adds a second engine and its HNSW persistence is experimental. SQLite keeps one file, one engine, stdlib driver. Decision in [ADR-002](../spec/decisions.md#adr-002-sqlite--sqlite-vec-with-numpy-fallback).

## 2. SQLite settings

- One DB file: `.state/repka.db`. `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000`.
- Migrations: numbered SQL files `src/repka/store/migrations/NNN_name.sql`, applied in order, version stored in `schema_version`. Never edit an applied migration; add a new one.
- Writes per run happen inside transactions per batch (e.g. one transaction per search page / enrichment batch), so an interrupted run keeps completed work.
- The dashboard (Phase 2) opens the DB read-only (`file:...?mode=ro` URI) except for domain management writes.

## 3. Embeddings

| Option | How | Pros | Cons |
|---|---|---|---|
| **Ollama** (default) | HTTP `POST http://localhost:11434/api/embed` `{"model": ..., "input": [texts]}` → `{"embeddings": [[...]]}` | Runs on Metal; nothing heavy in our venv; batch input; user may already have it | External daemon must be running; model pulled with `ollama pull` |
| MLX (`mlx-embeddings`) | separate venv + worker subprocess, same pattern as deciders | Fully in-repo | More setup; second ML stack to maintain |
| sentence-transformers on MPS | torch in a worker venv | Many models | Heavy |

Decision: **Ollama backend in Phase 1** behind an `Embedder` protocol; MLX worker is a Phase 2 option only if needed. Default model `nomic-embed-text` (768 dims) **(confirmed in [spike T04](../spikes/T04-ollama.md))**. nomic expects task prefixes: use `search_document: ` for repo texts and `search_query: ` for queries.

Rules:
- Every stored vector records `model` and `dim`. Similarity queries filter by model; vectors of two models are never mixed. Changing `embed.model` triggers re-embedding (old rows kept until replaced).
- Embedding input: `full_name + description + topics + README excerpt (≤ 2,000 chars)`; `input_hash` avoids recomputation.
- Embedding is a feature flag: `embed.enabled: false` by default.

## 4. Derived metrics (definitions are fixed here, used by digest and dashboard)

- `stars_delta_{N}d = stars(latest snapshot) − stars(baseline)`, where *baseline* is the most recent snapshot with `taken_at <= now − 0.9·N days`. **No baseline → `null`** (never lifetime stars). N ∈ {1, 7, 30}.
- `velocity = round( ln(1 + commits_30d) · (0.5 + active_weeks_12 / 24) + 0.5 · ln(1 + releases_90d), 2 )`. Nulls count as 0, except when *all three* inputs are null → velocity `null`.
- `stack` = sorted set of dependency names parsed from key files (`pyproject.toml` `[project].dependencies` + `[tool.poetry].dependencies`, `package.json` `dependencies` + `devDependencies`, `Cargo.toml` `[dependencies]`, `go.mod` `require`). Stack change = set difference between two parsed versions.

## Sources

- [sqlite-vss repo](https://github.com/asg017/sqlite-vss/) and [Datasette plugin page](https://datasette.io/plugins/sqlite-vss) (not in active development; successor sqlite-vec)
- [i-programmer: SQLite gets into vector search](https://i-programmer.info/news/84-database/17458-sqlite-gets-into-vector-search.html)
- [sqlite-vec package (pre-v1 warning)](https://pkg.go.dev/github.com/hanzoai/sqlite-vec)
- Ollama `/api/embed`, nomic task prefixes: from prior knowledge **(verify)**.
