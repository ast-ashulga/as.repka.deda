# repka wiki

**repka** (as.repka.deda) is a local macOS CLI that monitors GitHub repositories in user-defined technology domains, triages them with local decider models, keeps metric history and embeddings in SQLite, and sends batched morning/evening Telegram digests. It also ships a local Streamlit dashboard with a landscape map.

User guides (setup, configuration, running, troubleshooting) are in [`docs/`](../docs/getting-started.md). This wiki holds the design material behind the code.

## Start here

| If you are… | Read |
|---|---|
| checking **how it fits together** | [Architecture](spec/architecture.md) (layout, config keys, data model, CLI) · [Decisions (ADRs)](spec/decisions.md) |
| checking **what was asked** | [Requirements](spec/requirements.md) (translated original text, IDs, assumptions) |
| curious about a **feature design** | [Digest insights](spec/2026-11-10-digest-insights-design.md) · [Run observability](spec/2026-11-10-run-observability-design.md) |
| wondering **why a technical choice was made** | [Research notes](research/deciders.md) and [spike results](spikes/T02-sqlite-vec.md) (below) |
| looking for the **original plan and task board** | [Archive](archive/README.md) (historical, not maintained) |

"The reference project" in these notes is an earlier private prototype of the same decider and Telegram patterns. It is not published.

## Map

```
wiki/
├── README.md                     ← you are here
├── spec/
│   ├── requirements.md           original requirements (translated), R-IDs, assumptions
│   ├── architecture.md           the shared contract: paths, config, tables, types, CLI
│   ├── decisions.md              ADR-001 … ADR-008
│   ├── 2026-11-10-digest-insights-design.md   digest facet tags and the separate releases message
│   └── 2026-11-10-run-observability-design.md  run phase, progress and ETA, `repka status`
├── research/
│   ├── github-api.md             search limits, bisection, GraphQL enrichment, rate limits
│   ├── deciders.md               d1 / pplx / Clef / Jev, worker contract, triage questions
│   ├── storage-embeddings.md     sqlite-vec vs vss, fallback, Ollama, metric formulas
│   └── telegram-scheduling.md    user account (MCP) vs Bot API, launchd, digest slots
├── spikes/                       measured results behind some decisions
│   ├── T02-sqlite-vec.md         sqlite-vec vs numpy
│   ├── T03-deciders.md           d1-3B / d1-2B / Clef on a labelled sample
│   ├── T04-ollama.md             Ollama embedder: shapes, dimension, timing
│   └── T25-rehearsal.md          end-to-end rehearsal against real services (no Telegram)
└── archive/                      historical roadmap, agent guide and per-milestone plans (not maintained)
```
