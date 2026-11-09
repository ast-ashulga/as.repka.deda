# Architecture Decision Records

[← Wiki home](../README.md) · Prev: [architecture](architecture.md)

Format: context → decision → consequences. Status: `accepted` unless noted. To change a decision, add a new ADR that supersedes the old one; don't edit history.

## ADR-001: Python + uv CLI, patterned on `as.tegerun.cli`
- **Context:** The reference project already runs local deciders on Apple Silicon and talks to Telegram, with well-tested conventions.
- **Decision:** Python ≥ 3.12, `uv`, hatchling, argparse CLI `repka`, pytest + ruff (`select=["F"]`). Port the reference patterns listed in [reference-tegerun](../research/reference-tegerun.md).
- **Consequences:** Agents can read working code for every hard part. The main venv stays free of torch/mlx.

## ADR-002: SQLite + sqlite-vec, with numpy fallback
- **Context:** The requirements say "sqlite-vss or DuckDB". sqlite-vss is unmaintained; its successor is sqlite-vec (pre-v1). Extension loading may be disabled in some macOS Python builds.
- **Decision:** One SQLite file. Vectors are stored as float32 BLOBs in `embeddings` (source of truth). `vec0` index tables are optional and built from them when `sqlite_vec` loads; otherwise `store/vectors.py` falls back to brute-force numpy cosine. Spike [P1-T02](../plans/phase-1/M0-foundation.md#p1-t02-spike-sqlite-vec-loads-under-uv-python) decides whether the index is used at all.
- **Consequences:** Pin the exact sqlite-vec version. Never mix models: every vector row carries `model` and `dim`.
- **Status:** Spike [P1-T02](../spikes/T02-sqlite-vec.md): NO-GO for the `vec0` index (extension loads, but numpy is about 20-28x faster at 5k-50k vectors). numpy brute force is the default; sqlite-vec 0.1.9 stays pinned as an optional path. P1-T17 implemented numpy only (no `vec0` code path, YAGNI); the spike script and extension test keep the switch cheap.

## ADR-003: Local decider backends via worker subprocesses
- **Context:** Deciders need conflicting ML stacks (transformers versions, MLX).
- **Decision:** Copy the reference `classifier/` as `deciders/` with the same worker I/O contract. Default model `d1-3B`. A `clef_mlx_worker.py` is added only if spike [P1-T03](../plans/phase-1/M0-foundation.md#p1-t03-spike-local-decider-runs-on-this-mac) and task [P1-T16](../plans/phase-1/M3-triage.md#p1-t16-optional-clef-mlx-worker) succeed.
- **Consequences:** The main package never imports ML libraries. Revisions are pinned to full commit hashes; workers run offline by default; worker env has no secrets.

## ADR-004: "jev-like" means a local decider; hosted Jev is optional and off
- **Context:** Jev (TypeSafe) is hosted-only with a waitlist; sending repo data out conflicts with "local".
- **Decision:** No hosted backend in Phase 1. Config key `triage.allow_remote: false` exists so any future remote backend is refused unless explicitly enabled.
- **Consequences:** Triage quality depends on local models; the spike measures it on a labeled sample.

## ADR-005: Telegram via the user's own account (fast-mcp-telegram), ported from the reference
- **Context:** First draft chose the Bot API. On 2026-11-07 the user chose to send from their own account with a Telegram app (`api_id`/`api_hash`), default destination `"me"` (Saved Messages, which only a user account has), configurable `chat_id`/`topic_id`. The reference already runs this path on the same machine.
- **Decision:** `notify/telegram_user.py` + `notify/mcp_client.py`, ported from the reference's `notifier.py`/`mcp_client.py`: a `fast-mcp-telegram` stdio subprocess (`>=0.46,<0.47`), every call through `GuardedSession` with the allowlist `{"send_message"}` only, behind the synchronous `Sender` protocol (`send(parts) -> SendResult`). Own session `.sessions/telegram.session` via `repka notify login`.
- **Consequences:** The session file is full account access: `.sessions/` is 0700 and gitignored, credentials never reach logs, the DB or decider workers. A Bot API backend can be added later behind the same protocol.

## ADR-006: Scheduling = hourly launchd pass + slot logic inside the app
- **Context:** "Morning/evening digests", per-domain scan periodicity (Phase 2 UI edits it), a Mac that sleeps.
- **Decision:** One launchd agent runs `repka run` every hour. The app decides which domains are due and whether a digest slot is due (with coalescing and `max_lateness`). `repka schedule install` is an explicit command.
- **Consequences:** Changing a domain's `scan_every` or the digest slots needs no launchd change. A missed slot never produces two digests.

## ADR-007: GitHub data access = REST search + batched GraphQL enrichment
- **Context:** Search is limited to 30 req/min and 1,000 results/query; enrichment per repo via REST would cost many calls.
- **Decision:** REST search with matrix × date bisection; GraphQL `nodes(ids:)` batches of 50 for stars/forks/commits/releases/key-file blob oids; REST only for contributors and participation, with ETag caching and a 7-day refresh. Repos keyed by numeric id. No concurrency against GitHub.
- **Consequences:** A scan of a few thousand repos fits easily in the hourly budget. A secondary rate limit ends the run's GitHub part cleanly; work resumes next run via `search_cursor`.

## ADR-008: Phase 2 dashboard = Streamlit, reading the same SQLite
- **Context:** The requirements suggest Streamlit or FastAPI; the dashboard is local and single-user.
- **Decision:** Streamlit multipage app in `dashboard/` (optional dependency group `dashboard`), Plotly charts, umap-learn + scikit-learn HDBSCAN. It reads the DB read-only except for domain management, which writes `domains/*.yaml` through `repka.domain.schema` functions.
- **Consequences:** No API layer to maintain. Detailed design happens after Phase 1 is stable.
- **Amendment (P2-T03, Landscape Recompute):** The dashboard's read-only rule has one more documented exception, not just domain management. The Landscape page's explicit **Recompute** button opens a writable connection (`connect()`), runs `migrate()` (applies any pending migrations), and upserts one row into `landscape_cache` via `dashboard_data.store_landscape`. The projection itself is computed on a read-only connection first, and nothing is written when there are no embeddings. Every other dashboard path stays read-only; the Domains page writes only `domains/*.yaml` through `repka.domain.schema`. Known limit: `migrate()` checks the version before it takes the write lock, so if `repka run` (or `repka db migrate`) migrates at the same moment, both can try to apply the same migration. The loser's `CREATE` then fails on the existing table, its transaction rolls back, and the error is raised. The schema is not damaged and the next run is fine, but the migration is not race-safe. Recompute reports this as a database error. See [architecture](architecture.md) §4 for the table contract.
