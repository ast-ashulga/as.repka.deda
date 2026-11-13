# Archive: historical development notes

[← Wiki home](../README.md)

This folder keeps the planning documents that were used while repka was first built. They are **not maintained** and
they do **not describe the current workflow** for contributors; see `CONTRIBUTING.md` in the repository root and the
live documents under [../spec/](../spec/architecture.md) instead.

| Path | What it is |
|---|---|
| [plans/ROADMAP.md](plans/ROADMAP.md) | the original implementation roadmap: global constraints, task board, dependency graph, requirement coverage |
| [plans/AGENT-GUIDE.md](plans/AGENT-GUIDE.md) | the task protocol of the original build (agents claimed per-task GitHub issues, committed and pushed on their own). It applied to that build only |
| [plans/phase-1/](plans/phase-1/M0-foundation.md), [plans/phase-2/](plans/phase-2/M7-dashboard.md) | per-milestone task plans (interfaces, tests, acceptance criteria) |
| [plans/2026-11-10-digest-insights.md](plans/2026-11-10-digest-insights.md), [plans/2026-11-10-run-observability.md](plans/2026-11-10-run-observability.md) | implementation plans of two later features; their designs are in [../spec/](../spec/2026-11-10-digest-insights-design.md) |

Notes on reading these files:

- Task ids such as `P1-T07` and review-focus ids such as `RF3` are defined inside the roadmap and are used only here.
- The per-task GitHub issue links of the original board have been removed, so statuses are not tracked anywhere.
  Several plans mention an "earlier private prototype" (not published) as the source of ported patterns.
- Some plans refer to files that no longer exist (for example the throwaway spike scripts under `scripts/`) or that
  changed after the plan was written. Where plan and code disagree, the code and
  [../spec/architecture.md](../spec/architecture.md) win.
- The Russian text that these documents originally contained has been translated to English. The only Cyrillic
  left is a pair of Russian stop-words written as escapes inside a code sample.
