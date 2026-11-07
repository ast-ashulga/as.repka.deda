# Research: What to reuse from `as.tegerun.cli`

[← Wiki home](../README.md) · Related: [deciders](deciders.md) · [telegram & scheduling](telegram-scheduling.md)

Reference checkout: `/Users/andreyshulga/projects/experimental/jevs/as.tegerun.cli` (package `tegerun`, CLI `tege`). It is a read-only Telegram triage CLI: read whitelisted chats → classify with local deciders → forward matches. Same shape as this project: **collect → decide → notify**.

## Reuse map

| Reference | Reuse in repka | How |
|---|---|---|
| `classifier/models.yaml`, `workers/d1_worker.py`, `workers/pplx_worker.py`, `setup.sh`, `fetch.py` | `deciders/` | **Copy verbatim** (keep pinned revisions), rename paths. Worker contract stays identical. |
| `classifier/questions.yaml` | `deciders/questions.yaml` | Copy the **structure only** (`state_template`, `questions` with `type/instructions/criteria/enabled/needs_*`). Do **not** copy its contents: it holds a personal candidate profile. Write repo-triage questions from [deciders § 4](deciders.md#4-triage-questions-for-repositories-default-set). The `skills` block becomes the per-domain `profile`. |
| `src/tegerun/classify.py` (`load_questions`, `load_models` with 40-char revision check, `build_requests`, `run_worker`, `normalize_answer`, `collect_verdicts`, `WorkerError`) | `src/repka/triage/deciders.py` | Port with renames; replace `Item`/messages with repo items. Keep: subprocess with scratch cwd + `-I`, env without secrets, timeout, partial `out.json`, blame rules for failures. |
| `config.py` (`PROJECT_ROOT` / `$TEGE_HOME`, strict `load_*` per section, `ConfigError`, `worker_env()`) | `src/repka/config.py` | Same pattern; env var `$REPKA_HOME`. |
| `runner.py` (`run_pass` with injected boundaries, `PassReport`, `explain()`) | `src/repka/pipeline.py` | Same pattern: pure orchestration, boundaries injected, fakes in tests. |
| `runstate.py` (`RunLock` flock, atomic JSON writes, corrupt state never reset) | `src/repka/runlock.py` | Lock reused; JSON state replaced by SQLite tables. |
| `preflight.py` (`Finding` error/warning, numbered to-do list, `doctor`) | `src/repka/preflight.py` | Same UX: `repka doctor`; `repka run` runs preflight first. |
| `textutil.printable` | `src/repka/textutil.py` | Copy; add `html_escape`. |
| `notifier.py` + `mcp_client.py` (MCP user-account sender, guarded allowlist, splitting) | `src/repka/notify/telegram_user.py`, `notify/mcp_client.py` | Ported (see [ADR-005](../spec/decisions.md)); allowlist reduced to `send_message`. Splitting/escaping rules reused. |
| `cli.py` (argparse, `ConfigError`/`ValueError` → `error: ...` exit 2, `WorkerError` exit 1, stdout = results, stderr = progress) | `src/repka/cli.py` | Same conventions. |
| Tooling: uv, hatchling, `pytest` (`asyncio_mode=auto` not needed), `ruff` with `select=["F"]`, `tests/test_smoke.py` compiling every file | same | Copy config. |
| README launchd section | `repka schedule install` | Automated by a command instead of manual plist editing. |

## Conventions inherited (they become Global Constraints)

- Safety/feature flags default to **False**; sending needs two explicit switches.
- Tests need no network, models or Telegram.
- `uv.lock` committed.
- Model revisions pinned to full commit hashes.
- Untrusted text: strip control characters for terminal, escape for Telegram.
- stdout carries results (incl. `--json`), stderr carries progress/logs.
