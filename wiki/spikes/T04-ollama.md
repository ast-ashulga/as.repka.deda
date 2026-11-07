# Spike T04: Ollama embedder returns vectors

[← Wiki home](../README.md) · [ROADMAP](../plans/ROADMAP.md) · Issue [#4](https://github.com/ast-ashulga/as.repka.deda/issues/4) · Script: `scripts/spike_ollama.py`

**Verdict: GO.** Use Ollama `nomic-embed-text` at `http://localhost:11434/api/embed`, dimension **768**, float32. Task prefixes are not required (see below).

## Environment

| Item | Value |
|---|---|
| Machine | macOS (Darwin 27.0.0), arm64 |
| Python | 3.12.9 (uv-managed venv, `uv run`) |
| Ollama | 0.40.15, running at `localhost:11434` |
| Model | `nomic-embed-text:latest` (id `0a109f422b47`, 274 MB), pulled with `ollama pull nomic-embed-text` |

## Endpoint and response shape

Request: `POST {embed.url}/api/embed` with

```json
{"model": "nomic-embed-text", "input": ["text 1", "text 2", "text 3"]}
```

Response (one vector per input, in order):

```json
{
  "model": "nomic-embed-text",
  "embeddings": [[-0.0301765, 0.0411467, -0.1422961, "... 768 floats"], "..."],
  "total_duration": 816114541,
  "load_duration": 716083,
  "prompt_eval_count": 17667
}
```

- Keys seen: `embeddings`, `load_duration`, `model`, `prompt_eval_count`, `total_duration`.
- The batch size returned equals the input count (3 in, 3 out; 32 in, 32 out).
- Every vector has length **768**. Values are float32-range numbers.
- `GET {url}/api/version` returns `{"version": "0.40.15"}`.

## Sanity: related vs unrelated

Related pair: two texts about Model Context Protocol clients/servers. Unrelated: a sourdough recipe and a cookie recipe.

| Input form | cos(related) | max cos(unrelated) | Passes |
|---|---|---|---|
| no prefix | 0.8906 | 0.4462 | yes |
| `search_query:` / `search_document:` prefixes | 0.8467 | 0.4775 | yes |

The sanity check passes with and without the prefixes. The no-prefix margin is larger on this two-pair sample (0.44 against 0.37). That is too small a sample to prove prefixes hurt, so the decision is recorded as "not required" rather than "forbidden". T18 starts with no prefix and may switch if retrieval quality looks poor.

## Timing

Batch of 32 texts, each about 1,500 characters (wiki Markdown chunks, so offline), 3 runs on the warm model:

| Run | Wall time |
|---|---|
| 1 | 0.864 s |
| 2 | 0.824 s |
| 3 | 0.825 s |

Median 0.825 s per batch of 32, about **26 ms per text**. The model load on the first call is negligible (`load_duration` 0.7 ms, since it was already resident).

## Consequences for later tasks

- `embed.model` = `nomic-embed-text`, `embed.url` = `http://localhost:11434`, `embed.batch` = 32 are confirmed. The architecture defaults (§ 3.2) are unchanged.
- The vector dimension is 768. `embeddings.dim` stores it, and `vec0` (if T02 is GO) must be created with `float[768]`.
- T18 calls `/api/embed` with a list `input`. It does not use the legacy `/api/embeddings` endpoint.
- No fallback (MLX embedding worker) is needed.
