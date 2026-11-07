# Agent Guide: how to execute a task from this plan

[← Wiki home](../README.md) · [ROADMAP](ROADMAP.md)

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to work through this plan task by task. Within a task, follow `superpowers:test-driven-development`.

## 1. Picking a task

1. **GitHub Issues are the source of truth for status.** List the open tasks of the current milestone: `gh issue list --label task --state open --milestone "<milestone title>"` (or `--label in-progress` to see what is taken). Pick the first open issue that has neither the `in-progress` nor the `blocked` label and whose **Depends on** issues are all closed (`gh issue view <n>`). [ROADMAP § Task board](ROADMAP.md#task-board) is the static index with waves; tasks in the same "wave" have no dependencies on each other and can run in parallel (separate git worktrees).
2. Claim it: `gh issue edit <n> --add-label in-progress` and `gh issue comment <n> --body "Started by <agent name>"`. The ROADMAP file is not edited for status.
3. Open the task section in its milestone file. **Read in this order:** the task section → the [Global Constraints](ROADMAP.md#global-constraints) → the [architecture contract](../spec/architecture.md) sections the task links → research pages the task links. You do not need other tasks' bodies: each task's **Interfaces** block tells you the names and types its neighbours use.

## 2. Doing the task

- Work test-first: write the listed tests, see them fail, implement, see them pass.
- The plan fixes names, signatures, file paths, config keys and values. Everything it leaves open is your call — choose the simplest thing that satisfies the tests and the acceptance criteria.
- If the plan is **wrong** (an API behaves differently, a signature can't work), do not silently diverge: make the smallest fix, update the affected doc (architecture/ADR/task Interfaces) in the same change, and list the deviation in your report.
- If you are **blocked** (missing credential, hardware, an unresolved spike), swap the `in-progress` label for `blocked` (`gh issue edit <n> --remove-label in-progress --add-label blocked`), write the reason in an issue comment, and stop.
- Step checkboxes inside a task are your working notes. The **GitHub issue (labels + open/closed) is the only authoritative progress tracker.** Post anything a later reader needs (deviations, spike verdicts, blockers) as an issue comment as well as in the docs.
- **Parallel work:** when several agents run in one wave, each works in its own worktree on a branch `task/<id>` (e.g. `task/P1-T06`), commits and pushes that branch, and merges it into `main` (fast-forward or merge commit, no force) once its suite passes on top of the latest `main`. Status is tracked on the GitHub issues (each implementer updates only its own issue), so there is no shared status file to conflict on. Shared files such as `src/repka/cli.py` (subcommand registration) and `models.py` are edited by *appending*; resolve merge conflicts by keeping both sides.

## 3. Definition of Done (every task)

A task is `done` when all of these hold:

1. Every **Acceptance criterion** of the task is met.
2. `uv run pytest` passes (whole suite, not only new tests) and `uv run ruff check src tests` is clean.
3. No test touches the network, Telegram, GitHub or a model (fixtures/fakes only). Tests marked `@pytest.mark.live` are the only exception and are skipped by default.
4. New config keys / tables / commands are reflected in [architecture.md](../spec/architecture.md) and, if user-facing, in `README.md`.
5. New behaviour flags default to `false`.
6. Changes are **committed and pushed by the agent** — no user approval needed (see Global Constraints). One or more conventional commits per task (`feat(scope): ...`, `fix: ...`, `test: ...`, `docs: ...`, `chore: ...`) ending with the task id, e.g. `feat(domain): query matrix and domain plan (P1-T06)`. Reference the issue in every commit (`Refs #6`). Push with `git push origin <branch>`; if the push is rejected, `git pull --rebase` and push again. Never `--force`, never amend or rebase pushed commits. Commit only after the suite and ruff pass.
7. Issue labelled `review` (replace `in-progress`) with a comment holding the report below and the commit hash(es). A reviewer (human or `superpowers:requesting-code-review`) accepts it by closing the issue; the **last commit that completes the task carries `Closes #<n>`**, so it closes automatically once pushed to `main` — use `Closes` only when the review is not wanted separately, otherwise use `Refs` and close by hand after review. Review fixes are new commits on top, pushed the same way.

## 4. Report format (end of every task)

```
Task: P1-Txx <title>
Issue: #<n> · Status: review | blocked
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
| `todo` | open issue without a status label |
| `in-progress` | open + label `in-progress` (claim comment names the agent) |
| `blocked` | open + label `blocked`; reason in a comment |
| `review` | open + label `review`; implemented, committed and pushed |
| `done` | issue closed as completed (reviewed; fixes committed and pushed) |
| `dropped` | issue closed as *not planned* (`gh issue close <n> --reason "not planned"`) with the reason in a comment, e.g. a gated optional task whose spike failed |

## 6. Conventions inherited from the reference project

- Package layout `src/repka/`; run everything with `uv run ...` from the repo root.
- `ConfigError` for anything the user can fix (exit 2); `WorkerError`/network failures exit 1.
- stdout = results (and `--json`); stderr = progress/logs.
- Untrusted text (repo names, descriptions, READMEs, release notes): `textutil.printable()` before printing, `textutil.html_escape()` before Telegram.
- Use the reference code as a source to port from: `/Users/andreyshulga/projects/experimental/jevs/as.tegerun.cli` (see [reference map](../research/reference-tegerun.md)). Don't copy `classifier/questions.yaml` contents or `.env`.
