# Agent Guide: how to execute a task from this plan

[← Wiki home](../README.md) · [ROADMAP](ROADMAP.md)

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to work through this plan task by task. Within a task, follow `superpowers:test-driven-development`.

## 1. Picking a task

1. Open [ROADMAP.md § Task board](ROADMAP.md#task-board). Pick the first task whose **Status** is `todo` and whose **Depends on** tasks are all `done`. Tasks in the same "wave" have no dependencies on each other and can run in parallel (separate git worktrees).
2. Set its status to `in-progress` and put your agent name in **Owner** (the only line you edit in ROADMAP at this point).
3. Open the task section in its milestone file. **Read in this order:** the task section → the [Global Constraints](ROADMAP.md#global-constraints) → the [architecture contract](../spec/architecture.md) sections the task links → research pages the task links. You do not need other tasks' bodies: each task's **Interfaces** block tells you the names and types its neighbours use.

## 2. Doing the task

- Work test-first: write the listed tests, see them fail, implement, see them pass.
- The plan fixes names, signatures, file paths, config keys and values. Everything it leaves open is your call — choose the simplest thing that satisfies the tests and the acceptance criteria.
- If the plan is **wrong** (an API behaves differently, a signature can't work), do not silently diverge: make the smallest fix, update the affected doc (architecture/ADR/task Interfaces) in the same change, and list the deviation in your report.
- If you are **blocked** (missing credential, hardware, an unresolved spike), set the status to `blocked`, write the reason in **Notes**, and stop.
- Step checkboxes inside a task are your working notes. The **ROADMAP status column is the only authoritative progress tracker.**
- **Parallel work:** when several agents run in one wave, each works in its own worktree on a branch `task/<id>` (e.g. `task/P1-T06`), commits and pushes that branch, and merges it into `main` (fast-forward or merge commit, no force) once its suite passes on top of the latest `main`. Only the controller in the main checkout edits ROADMAP; implementers report status instead of editing it. Shared files such as `src/repka/cli.py` (subcommand registration) and `models.py` are edited by *appending*; resolve merge conflicts by keeping both sides.

## 3. Definition of Done (every task)

A task is `done` when all of these hold:

1. Every **Acceptance criterion** of the task is met.
2. `uv run pytest` passes (whole suite, not only new tests) and `uv run ruff check src tests` is clean.
3. No test touches the network, Telegram, GitHub or a model (fixtures/fakes only). Tests marked `@pytest.mark.live` are the only exception and are skipped by default.
4. New config keys / tables / commands are reflected in [architecture.md](../spec/architecture.md) and, if user-facing, in `README.md`.
5. New behaviour flags default to `false`.
6. Changes are **committed and pushed by the agent** — no user approval needed (see Global Constraints). One or more conventional commits per task (`feat(scope): ...`, `fix: ...`, `test: ...`, `docs: ...`, `chore: ...`) ending with the task id, e.g. `feat(domain): query matrix and domain plan (P1-T06)`. Push with `git push origin <branch>`; if the push is rejected, `git pull --rebase` and push again. Never `--force`, never amend or rebase pushed commits. Commit only after the suite and ruff pass.
7. ROADMAP status set to `review` with a one-line note and the commit hash(es); a reviewer (human or `superpowers:requesting-code-review`) moves it to `done`. Review fixes are new commits on top, pushed the same way.

## 4. Report format (end of every task)

```
Task: P1-Txx <title>
Status: review | blocked
Changed files: <list>
Tests: <command> → <N passed>
Acceptance: <each criterion → met / not met (why)>
Deviations from plan: <none | list + docs updated>
Commits: <hash> <message> (pushed to origin/<branch>)
Follow-ups: <none | list>
```

## 5. Status values

| Status | Meaning |
|---|---|
| `todo` | not started |
| `in-progress` | an agent is working on it (Owner set) |
| `blocked` | cannot proceed; reason in Notes |
| `review` | implemented, committed and pushed; waiting for review |
| `done` | reviewed and accepted (fixes, if any, committed and pushed) |
| `dropped` | intentionally not done (reason in Notes), e.g. a gated optional task whose spike failed |

## 6. Conventions inherited from the reference project

- Package layout `src/repka/`; run everything with `uv run ...` from the repo root.
- `ConfigError` for anything the user can fix (exit 2); `WorkerError`/network failures exit 1.
- stdout = results (and `--json`); stderr = progress/logs.
- Untrusted text (repo names, descriptions, READMEs, release notes): `textutil.printable()` before printing, `textutil.html_escape()` before Telegram.
- Use the reference code as a source to port from: `/Users/andreyshulga/projects/experimental/jevs/as.tegerun.cli` (see [reference map](../research/reference-tegerun.md)). Don't copy `classifier/questions.yaml` contents or `.env`.
