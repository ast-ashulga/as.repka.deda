# repka wiki

**repka** (as.repka.deda) is a local macOS CLI that monitors GitHub repositories in user-defined technology domains, triages them with local decider models, keeps metric history and embeddings in SQLite, and sends batched morning/evening Telegram digests. Phase 2 adds a local dashboard with a landscape map.

## Start here

| If you are… | Read |
|---|---|
| an **AI agent implementing a task** | [Agent guide](plans/AGENT-GUIDE.md) → [ROADMAP task board](plans/ROADMAP.md#task-board) → your task section |
| reviewing the **plan** | [ROADMAP](plans/ROADMAP.md) (goal, global constraints, review focus, task board, dependency graph, coverage) |
| checking **what was asked** | [Requirements](spec/requirements.md) (original RU text, IDs, assumptions) |
| checking **how it fits together** | [Architecture](spec/architecture.md) (layout, config, data model, CLI) · [Decisions (ADRs)](spec/decisions.md) |

## Map

```
wiki/
├── README.md                     ← you are here
├── spec/
│   ├── requirements.md           original requirements, R-IDs, assumptions
│   ├── architecture.md           the shared contract: paths, config, tables, types, CLI
│   └── decisions.md              ADR-001 … ADR-008
├── research/
│   ├── github-api.md             search limits, bisection, GraphQL enrichment, rate limits
│   ├── deciders.md               d1 / pplx / Clef / Jev, worker contract, triage questions
│   ├── storage-embeddings.md     sqlite-vec vs vss, fallback, Ollama, metric formulas
│   ├── telegram-scheduling.md    user account (MCP) vs Bot API, launchd, digest slots
│   └── reference-tegerun.md      what to reuse from as.tegerun.cli
├── plans/
│   ├── ROADMAP.md                ★ status tracker (single source of truth)
│   ├── AGENT-GUIDE.md            how agents pick, do, report tasks; Definition of Done
│   ├── phase-1/
│   │   ├── M0-foundation.md      T01 scaffold · T02–T04 spikes
│   │   ├── M1-domain-profile.md  T05 schema · T06 matrix · T07 wizard · T08 Claude skill
│   │   ├── M2-collector.md       T09 store · T10 client · T11 search · T12 enrich · T13 collect
│   │   ├── M3-triage.md          T14 decider runtime · T15 triage · T16 Clef (optional)
│   │   ├── M4-embeddings.md      T17 vectors · T18 Ollama embed/similar
│   │   ├── M5-digest.md          T19 metrics+builder · T20 Telegram · T21 slots
│   │   └── M6-orchestration.md   T22 run · T23 launchd · T24 doctor · T25 E2E+README
│   └── phase-2/
│       └── M7-dashboard.md       P2-T01…T04 (task level; refined after Gate G1)
└── spikes/                       results written by spike tasks (T02, T03, T04, T25)
```

## Phases at a glance

1. **Phase 1 — CLI core & pipeline** (M0–M6, 25 tasks): domain profiles → GitHub collector → decider triage → SQLite + vectors → Telegram digests → launchd daemon.
2. **Gate G1** — 7 days of stable unattended runs + triage spot-check.
3. **Phase 2 — Dashboard** (M7, 4 tasks): domain management UI, landscape map (UMAP/t-SNE + HDBSCAN), analytics charts (stars_delta, velocity, stack changes).
