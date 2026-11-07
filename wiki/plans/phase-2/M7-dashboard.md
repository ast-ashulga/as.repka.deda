# M7: Dashboard & landscape visualization (Phase 2)

[← ROADMAP](../ROADMAP.md) · Prev: [M6](../phase-1/M6-orchestration.md) · Prerequisite: [Gate G1](../ROADMAP.md#gate-g1-phase-1-is-stable)

> **Planning depth:** Phase 2 is planned at task level (goal, inputs, outputs, acceptance criteria). The requirements say it starts only after Phase 1 is stable, so its detailed steps are written when Gate G1 passes. **Before starting any P2 task, an agent first refines it** in this file (Files / Interfaces / Steps, same format as Phase 1), using what Phase 1 actually built, and gets the refinement approved.

Decision: Streamlit multipage app, Plotly, umap-learn, scikit-learn (HDBSCAN, t-SNE) — [ADR-008](../../spec/decisions.md#adr-008-phase-2-dashboard--streamlit-reading-the-same-sqlite). Dependencies go in an optional group `dashboard` (`uv sync --group dashboard`) so the daemon's environment stays small. Launch: `repka dashboard` → `streamlit run dashboard/app.py` bound to `127.0.0.1` only.

---

### P2-T01: Dashboard skeleton and data layer

**Req:** R-2.1, R-2.2, R-2.3 · **Depends on:** Gate G1 · **Size:** M
**Goal:** A local Streamlit app with navigation and a tested, read-only data layer that all pages share.

- **Inputs:** `store.db.connect(readonly=True)`, `store.metrics`, `store.vectors.load_matrix`, `domain.schema.load_domains`.
- **Outputs:** `dashboard/app.py` (home: domains, last runs, last digests), `src/repka/dashboard_data.py` with pure functions returning pandas DataFrames (`domains_overview()`, `repos_frame(domain_id, accepted_only)`, `metrics_history(repo_ids)`, `stack_history(repo_id)`), `repka dashboard [--port 8501]` command.
- **Acceptance criteria:**
  - Data functions unit-tested on a fixture DB; pages contain no SQL.
  - Server binds to `127.0.0.1`; the dashboard opens the DB read-only (writes are only possible through P2-T02's domain functions).
  - Works while `repka run` is writing (WAL), with no "database is locked" errors in a 10-minute concurrent test.

### P2-T02: Domain management page

**Req:** R-2.1 · **Depends on:** P2-T01 · **Size:** M
**Goal:** Add, edit, enable/disable, delete domains and change `scan_every` from the browser.

- **Inputs:** `Domain`, `save_domain`, `delete_domain`, `load_domains`, `build_queries`, `suggest_keywords` (P1-T05..T07).
- **Outputs:** `dashboard/pages/1_Domains.py`: table of domains with status (last scan, repos, accepted); form editor with validation errors shown inline; query-matrix preview; delete with confirmation (history in DB kept; offer "also purge data" as a separate, explicit, off-by-default checkbox).
- **Acceptance criteria:**
  - Every write goes through `repka.domain.schema` (same validation as the CLI); an invalid form never writes a file.
  - Changing `scan_every` takes effect on the next hourly `repka run` without touching launchd.
  - New domains are created with `enabled: false` unless the user ticks "enable".

### P2-T03: Landscape map

**Req:** R-2.2 · **Depends on:** P2-T01 · **Size:** L
**Goal:** An interactive 2-D map of a domain's semantic space showing where the main players cluster and where niche forks sit.

- **Inputs:** `load_matrix(conn, model, repo_ids)`, repo attributes (stars, is_fork, parent, kind, velocity).
- **Outputs:** `src/repka/landscape.py` (pure: `project(matrix, method: "umap"|"tsne", seed) -> ndarray`, `cluster(points_or_matrix, min_cluster_size) -> labels`, `label_clusters(frame) -> dict[label, top_terms]` from topics/descriptions); cached results in a table `landscape_cache(domain_id, model, method, params_hash, computed_at, payload)` via a new migration; `dashboard/pages/2_Landscape.py` with Plotly scatter (size = stars, colour = cluster, symbol = fork vs original, hover = name/description/kind), filters (accepted only, include forks, min stars), click-through to GitHub.
- **Acceptance criteria:**
  - Deterministic with a fixed seed (test on synthetic clustered vectors: ≥ 90 % of points of each synthetic cluster share an HDBSCAN label).
  - Forks need data: if the domain's `fork_policy` is `exclude`, the page says so and links to the domain editor.
  - 5,000 repos render in < 10 s on Apple Silicon (UMAP cached; recompute only when vectors change).
  - Only vectors of one embedding model are ever projected together.

### P2-T04: Analytics charts

**Req:** R-2.3 · **Depends on:** P2-T01 · **Size:** M
**Goal:** Track star growth, development velocity and tech-stack changes per project and per domain.

- **Inputs:** `snapshots`, `store.metrics.stars_delta/velocity`, `key_files.deps_json`, `events(kind="stack_changed")`.
- **Outputs:** `dashboard/pages/3_Analytics.py`: (a) stars and `stars_delta_7d` time series for selected repos; (b) velocity ranking table + sparkline per repo; (c) "top movers" over a chosen window; (d) stack view: dependency timeline per repo (added/removed per key-file version) and domain-level dependency popularity over time.
- **Acceptance criteria:**
  - Metric values on the page equal `store.metrics` outputs (shared code, tested on a fixture DB).
  - Gaps in snapshots are shown as gaps, not interpolated; `null` deltas are shown as "n/a".
  - Charts follow the `dataviz` skill guidance (consistent palette, light/dark legible).

### Possible later tasks (not scheduled)

- Bot API Telegram backend behind the same `Sender` protocol (ADR-005 alternative).
- Per-domain Telegram routing (chat per domain).
- MLX embedding worker if Ollama is unwanted.
- Hosted Jev backend behind `triage.allow_remote` (ADR-004).
