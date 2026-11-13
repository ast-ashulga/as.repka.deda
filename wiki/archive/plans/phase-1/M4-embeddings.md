# M4: Embeddings & vector store

[← ROADMAP](../ROADMAP.md) · Prev: [M3](M3-triage.md) · Next: [M5 Digest](M5-digest.md)

Goal of the milestone: accepted repos get semantic embeddings (local Ollama), stored in SQLite with model tags; nearest-neighbour search works with sqlite-vec or the numpy fallback. Phase 2's landscape map reads these vectors.

Research: [storage-embeddings](../../../research/storage-embeddings.md) · ADR: [ADR-002](../../../spec/decisions.md#adr-002-sqlite--sqlite-vec-with-numpy-fallback) · Spike results: `wiki/spikes/T02-sqlite-vec.md`, `wiki/spikes/T04-ollama.md`

---

### P1-T17: Vector store with fallback

**Req:** R-1.4 · **Depends on:** T02, T09 · **Size:** S
**Goal:** Store vectors as BLOBs (source of truth) and answer KNN queries through `vec0` when available, numpy otherwise, with identical results.

**Files:**
- Create: `src/repka/store/vectors.py`
- Test: `tests/test_vectors.py`

**Interfaces:**
- Consumes: `store.db.connect`; `embeddings` table (T09); spike T02 verdict and pinned `sqlite-vec`.
- Produces (`repka.store.vectors`):
  - `to_blob(vec: Sequence[float]) -> bytes` (float32 little-endian) · `from_blob(b: bytes, dim: int) -> np.ndarray`.
  - `upsert_embedding(conn, repo_id: int, model: str, vector: Sequence[float], input_hash: str, now: str) -> None` (`dim` = len(vector); raises `ValueError` if the model already has rows of another dim).
  - `embedding_hash(conn, repo_id: int, model: str) -> str | None`.
  - `load_matrix(conn, model: str, repo_ids: Iterable[int] | None = None) -> tuple[list[int], np.ndarray]` — ids and an L2-normalized `(n, dim)` float32 matrix (used by Phase 2).
  - `class VectorIndex(Protocol)`: `knn(query: Sequence[float], k: int, candidates: set[int] | None = None) -> list[tuple[int, float]]` → `(repo_id, cosine_similarity)` sorted desc.
  - `NumpyIndex(conn, model)` (loads the model's matrix once); `get_index(conn, model: str) -> VectorIndex` (returns `NumpyIndex`). No `SqliteVecIndex`, `vec_available` or `prefer_vec` (see Step 3 note).

- [ ] **Step 1: Write failing tests**

```python
def test_blob_roundtrip()
def test_dim_mismatch_for_same_model_raises()
def test_models_are_isolated()                      # model B vectors never returned for model A queries
def test_numpy_knn_orders_by_cosine()
def test_vec_and_numpy_agree_on_top_k()             # 500 random 64-dim vectors, k=10; skip if not vec_available
def test_candidates_filter_restricts_results()
def test_get_index_falls_back_when_extension_missing(monkeypatch)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** If the T02 verdict was NO-GO, implement `SqliteVecIndex` anyway only if trivial; otherwise leave it out, keep `get_index` returning `NumpyIndex`, and note it. **Step 4: Run** → PASS.
  - *Outcome (P1-T17):* T02 was NO-GO, so the vec0 path was dropped together with its two tests (`test_vec_and_numpy_agree_on_top_k`, `test_get_index_falls_back_when_extension_missing`). `get_index(conn, model)` takes no `prefer_vec` argument.
- [ ] **Step 5: Commit & push**, report. Message: `feat(store): vector store with sqlite-vec and numpy fallback`.

**Acceptance criteria:** identical top-k from both paths (when vec is available); vectors of different models never mix.

---

### P1-T18: Ollama embedder, `repka embed` and `repka similar`

**Req:** R-1.4, R-G2 · **Depends on:** T04, T15 (stores `readme_excerpt`), T17 · **Size:** M
**Goal:** Compute embeddings for accepted repos with Ollama and expose a nearest-neighbour debug command.

**Files:**
- Create: `src/repka/embed/__init__.py`, `src/repka/embed/ollama.py`, `src/repka/embed/embed.py`, `src/repka/embed/cli.py`
- Modify: `src/repka/cli.py`
- Test: `tests/test_embed.py`

**Interfaces:**
- Consumes: `EmbedConfig`; store `repos_for_domain(accepted_only=True)`, `upsert_embedding`, `embedding_hash`, `get_index`; `repos.readme_excerpt` (stored by T15); spike T04 values (dim, prefixes).
- Produces:
  - `repka.embed.ollama`: `class EmbedderUnavailable(RuntimeError)`; `class Embedder(Protocol)`: `model: str`, `embed(texts: list[str]) -> list[list[float]]`; `OllamaEmbedder(url: str, model: str, timeout_s: float = 60, transport: httpx.BaseTransport | None = None)` calling `POST {url}/api/embed` `{"model", "input"}` → `embeddings`. Connection refused → `EmbedderUnavailable("Ollama is not running at <url>: start it with `ollama serve`")`; model not found → `ConfigError("embedding model <m> missing: run `ollama pull <m>`")`.
  - `repka.embed.embed`: `DOC_PREFIX = "search_document: "`, `QUERY_PREFIX = "search_query: "` (values from spike T04; empty strings if the model needs none); `embedding_text(row) -> str` (`full_name`, description, topics, README excerpt ≤ 2,000 chars, through `printable`); `input_hash(text: str) -> str`; `@dataclass EmbedStats(pending: int = 0, embedded: int = 0)`; `embed_domain(conn, domain_id: str, embedder: Embedder, batch: int, now: str) -> EmbedStats` (accepted repos whose `embedding_hash != input_hash(text)`; commit per batch).
  - CLI `repka embed [--domain ID]` (explicit command works even if `embed.enabled` is false) and `repka similar <owner/name> [-k 10] [--domain ID] [--json]` (uses the repo's stored vector; table `similarity · full_name · description`).

- [ ] **Step 1: Write failing tests** (Ollama via `httpx.MockTransport`; `FakeEmbedder` hashing text to deterministic vectors):

```python
def test_ollama_embed_batches_and_parses()
def test_connection_refused_gives_hint()
def test_model_missing_is_config_error()
def test_embed_domain_only_accepted_and_changed()
def test_second_run_embeds_nothing()
def test_model_change_reembeds_and_keeps_old_rows()
def test_doc_prefix_applied()
def test_similar_cli_lists_neighbours(home, capsys)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
  - *Outcome (P1-T18):* `DOC_PREFIX` and `QUERY_PREFIX` are empty strings (T04: prefixes not required). `repka embed` also takes `--json`. The embed hash covers the prefixed text, so a prefix change re-embeds. Ollama down → `error:` + exit 1; model missing → exit 2.
- [ ] **Step 5: Manual check:** after T15's manual triage, `uv run repka embed --domain <id>` then `uv run repka similar <a known accepted repo>`; paste top 5 into the report with a judgement.
- [ ] **Step 6: Commit & push**, report. Message: `feat(embed): ollama embeddings, embed and similar commands`.

**Acceptance criteria:** embeddings computed only for accepted repos whose text changed; clear error messages when Ollama is down or the model is missing; no network in tests.
