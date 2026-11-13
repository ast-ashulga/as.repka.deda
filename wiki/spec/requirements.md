# Requirements

[← Wiki home](../README.md) · Next: [architecture](architecture.md) · [decisions](decisions.md)

## 1. Original requirements (translated from the original Russian text)

> **1. Project goal and principles**
>
> Goal: Build a local utility that automatically monitors, tracks trends in and analyses GitHub repositories in given technology domains.
>
> Target platform: macOS (optimised for running local models on Apple Silicon via MLX/Ollama, data stored in local files).
>
> Alerting philosophy: No real-time spam alerts. Only batched, regular digests.
>
> **2. Phased implementation plan**
>
> **Part 1: CLI core and backend pipeline (no dashboard)**: a standalone script/daemon that collects data, filters it and sends reports.
> - Interviewing and query generation (Domain Profile Skill): a script (or an interactive prompt) builds a GitHub search matrix from a detailed description of the area.
> - Data collection from GitHub (Collector Engine): GitHub Search API with pagination and partitioning by date/language to get around its limits. Metrics collected: stars, forks, contributors, commit regularity, new releases and changed key files (pyproject.toml, package.json).
> - Triage and classification (Deciders models): instead of general-purpose LLMs, lightweight Deciders models (jev-like, clef and similar) for binary triage and fast classification: "Is this relevant to our domain? Is there a ready-made tool/protocol here, or is it junk/an empty shell?".
> - Storage (Local Data Store): a local SQLite database plus a vector extension (sqlite-vss or DuckDB) for metric history and semantic embeddings.
> - Report delivery (Telegram): structured batched digests (morning/evening) to a Telegram chat.
>
> **Part 2: Dashboard and landscape visualisation**: after Part 1 runs stably.
> - Domain management interface: a local web application (Streamlit or FastAPI) for adding, removing and changing the scan frequency of areas.
> - Interactive landscape map: UMAP/t-SNE plus clustering over embeddings; where the main players cluster, where the niche forks are.
> - Analytics charts: star growth dynamics (stars_delta), activity indices (Velocity), technology stack changes per project.
>
> Note: for triage by local models and Telegram access there is an example: an earlier private prototype. Use similar approaches where they fit.

## 2. Requirement IDs (traceability)

Every task in the [archived plan](../archive/plans/ROADMAP.md) cites the IDs it implements. Coverage matrix: [ROADMAP § coverage](../archive/plans/ROADMAP.md#requirement-coverage).

| ID | Requirement (EN) | Interpretation / acceptance |
|---|---|---|
| **R-G1** | Local utility for monitoring/trend tracking/analysis of GitHub repos in configured technology domains | Single Python CLI `repka`, all data in the project folder. |
| **R-G2** | macOS, local models on Apple Silicon via MLX/Ollama, local files | Deciders run on MPS/MLX; embeddings via Ollama; SQLite file. Linux not a goal. |
| **R-G3** | No real-time alerts, only batched regular digests | Nothing is sent outside digest slots; empty digests are not sent. |
| **R-1.1** | Domain Profile: interview → GitHub search matrix from a detailed domain description | `repka domain new` (interactive wizard) + Claude Code skill `domain-profile`; output `domains/<id>.yaml`; `repka domain plan <id>` shows the expanded query matrix. |
| **R-1.2a** | Collector: Search API with pagination and date/language partitioning to get around limits | Query matrix × date bisection, ≤1000 results per slice, resumable. |
| **R-1.2b** | Metrics: stars, forks, contributors, commit regularity, new releases, changed key files | Snapshots per scan; events for new releases and key-file changes; dependency stack parsed. |
| **R-1.3** | Triage with lightweight decider models (binary + quick classification) | `relevant` (noul), `substance` (score), `kind` (choice) via local deciders; thresholds. |
| **R-1.4** | SQLite + vector extension for metric history and embeddings | SQLite + sqlite-vec (fallback numpy); history tables; embeddings tagged with model. |
| **R-1.5** | Telegram batched digests morning/evening | User-account sender (fast-mcp-telegram), slots, coalescing, dedupe of reported items. |
| **R-1.6** | Autonomous script/daemon | `repka run` one pass; launchd agent hourly; lock; preflight/doctor. |
| **R-2.1** | Local web UI to add/remove domains and change scan periodicity | Streamlit app page "Domains". |
| **R-2.2** | Landscape map: UMAP/t-SNE + clustering over embeddings | Streamlit page with interactive scatter, clusters, forks highlighted. |
| **R-2.3** | Charts: stars_delta, Velocity, tech stack changes | Streamlit page with time series and stack diff. |

## 3. Clarifications and assumptions (made by the planner, revisit if wrong)

| # | Assumption | Why |
|---|---|---|
| A1 | Language: Python ≥ 3.12, managed by `uv`; CLI name `repka`, package `repka`. | Matches reference project; ML ecosystem. |
| A2 | "jev-like, clef" = local decision models with noul/choice/score answers. Default model is LiquidAI d1-3B (proven locally); Clef via MLX is optional after a spike; hosted Jev is an off-by-default remote backend (maybe never built). | Requirement says local; Jev weights are not public. |
| A3 | sqlite-vss is replaced by sqlite-vec (successor by the same author); DuckDB not used. | vss unmaintained. |
| A4 | Telegram delivery uses the user's own account (Telegram app `api_id`/`api_hash`, fast-mcp-telegram), default chat `"me"`. | User's choice (2026-11-07); same path as the reference project. |
| A5 | One digest covers all domains, grouped by domain; per-domain chat routing is a later option. | Simplest useful default. |
| A6 | GitHub token (fine-grained PAT, public read) is available. | Search API needs auth for 30 req/min. |
| A7 | Scale: up to ~10 domains × a few thousand repos each. | Drives the choice of brute-force fallback and simple scheduling. |
| A8 | Phase 2 is planned at task level only; it is refined after Phase 1 runs stably. | Requirements say Phase 2 follows Phase 1 stabilization. |
