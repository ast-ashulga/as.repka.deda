# Run Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work test-first inside each task (superpowers:test-driven-development).

**Goal:** While `repka run` works, show which stage and step is running, how many units are done of how many, elapsed time and an honest ETA, to a person (terminal, log) and to an agent (JSONL stream, status file, `repka status`).

**Architecture:** A stdlib-only `repka.progress` package: a `Reporter` event protocol, a `RunProgress` state machine (snapshot, ETA, timings), four stderr renderers (`plain` = today, `live`, `lines`, `jsonl`) and an atomic `.state/progress.json` writer. `run_pass`, `collect_domains`, `triage_domain` and `embed_domain` emit events through additive optional parameters; `run_cli` selects the renderer and stores `timings` in `runs.stats_json`.

**Tech Stack:** Python 3.12 stdlib, argparse, SQLite (no migration), pytest, ruff, `uv`.

**Spec:** `wiki/spec/2026-11-10-run-observability-design.md` (read first; this plan only fixes what it leaves open).

## Global Constraints

- Selector `run.progress`: `plain | live | lines | jsonl`, default `plain`; `repka run --progress MODE` overrides; strict enum, `ConfigError` names `run.progress`. No other new flag. `live` on a non-TTY falls back to `lines`.
- `plain` output is byte-for-byte today's (`run_cli._progress` lines). stdout is unchanged in every mode.
- In `jsonl` mode every stderr line is a JSON object (logs, notes, CLI messages, summary included).
- Constants in one place (`progress/eta.py`): `MIN_ELAPSED_S = 3`, `EWMA_ALPHA = 0.3`, `LINES_INTERVAL_S = 10`, `JSONL_INTERVAL_S = 1`, `HEARTBEAT_S = 5`, `STALE_HEARTBEAT_S = 120`, `HISTORY_RUNS = 5`.
- A reporter or status-file error never fails a stage: swallow, `log.debug`.
- Untrusted text (domain ids, repo names, details) goes through `textutil.printable()` before any output.
- Tests never touch network/GitHub/Telegram/Ollama/model; inject the clock (`clock: Callable[[], float]`) and streams; no `time.sleep` in tests.
- Definition of Done per task (AGENT-GUIDE §3): `uv run pytest -q` and `uv run ruff check src tests` pass; new keys/commands mirrored in `wiki/spec/architecture.md`.
- Commit per task on branch `feat/run-observability`; project rule lets agents commit; never push to or merge into `main` without the owner's approval (owner's /goal instruction).
- Model per task (AGENT-GUIDE §2a): Haiku for Task 6 docs; Sonnet advised by Opus for Tasks 1-5.

## Review Focus

1. A non-TTY stderr with `--progress live` must not emit escape codes or `\r` (falls back to `lines`) (Task 2).
2. A log warning or worker output arriving mid-run must not corrupt the `live` line nor produce a non-JSON line in `jsonl` (Task 2, 4).
3. `progress.json` with `status: running` but no live lock holder (process killed) is reported "not running", never as live (Task 3).
4. Ctrl-C / crash leaves a terminated live line and a final `failed` status file (Task 5).
5. History from old runs without `timings`, or a stage that did no work, must not poison or crash the ETA (Task 1).

---

### Task 1: Progress core, ETA, timings

**Files:**
- Create: `src/repka/progress/__init__.py`, `src/repka/progress/events.py`, `src/repka/progress/eta.py`
- Test: `tests/test_progress_core.py`

**Interfaces:**
- Produces (`eta.py`): the constants above; `fmt_duration(seconds: float) -> str` (`45s`, `2m10s`, `1h05m`); `class RateEstimator(clock)` with `reset(total: int | None)`, `update(done: int)`, `rate() -> float | None`, `eta_seconds(done: int, total: int | None) -> float | None` (None until `done > 0` and `MIN_ELAPSED_S` elapsed); `history_medians(rows: Iterable[str | None]) -> dict[str, float]` (input: `stats_json` strings newest first; ignores None/invalid/missing `timings` and stages with `items == 0`; median of at most `HISTORY_RUNS` samples per stage).
- Produces (`events.py`): `Reporter` Protocol (`plan(stages: list[str])`, `stage_start(stage: str, steps: int | None = None)`, `step(label: str, unit: str = "", total: int | None = None)`, `advance(done: int, total: int | None = None, detail: str = "")`, `stage_end(status: Literal["ok","error","skipped"], detail: str = "")`, `note(message: str)`, `finish(status: str)`); `NullReporter`; `RunProgress(clock, history: dict[str, float] | None = None, sinks: list[Sink] = ())` implementing `Reporter`, with `snapshot() -> dict` (fields of spec D7: `status`, `started_at`, `stages[]`, `current`, `overall`) and `timings() -> dict[str, dict[str, float]]` (`{stage: {"seconds", "items"}}`). `Sink` is a Protocol with `on_event(event: str, snapshot: dict, payload: dict) -> None`; every Reporter call also fans out to sinks (exceptions swallowed).

- [ ] **Step 1: Write failing tests** in `tests/test_progress_core.py` with a `FakeClock` (`now` attribute, callable): `test_fmt_duration`; `test_eta_none_until_min_elapsed_and_done` (done=0 -> None; done=5 at t=2 -> None; at t=10 with 5/20 -> about 30s); `test_ewma_follows_slowdown`; `test_overall_eta_adds_history_for_later_stages` (plan collect, triage, digest; history `{"digest": 20}`; mid-triage overall = triage eta + 20); `test_overall_is_lower_bound_when_a_later_stage_has_no_history` (`eta_is_lower_bound is True`); `test_overall_unknown_when_nothing_known` (`eta_seconds is None`); `test_stage_eta_uses_mean_step_time_for_remaining_steps`; `test_history_medians_ignores_old_rows_and_idle_stages` (rows: None, `"{}"`, bad json, `{"timings": {"triage": {"seconds": 0.1, "items": 0}}}` ignored; three real rows -> median); `test_timings_sum_items_per_stage`; `test_sink_error_is_swallowed`.
- [ ] **Step 2: Run** `uv run pytest tests/test_progress_core.py -q` -> FAIL (module missing).
- [ ] **Step 3: Implement** `eta.py` then `events.py` to the interfaces. Items of a stage = sum of the `done` reached by each of its steps. `eta_at` = wall-clock epoch from an injectable `wall` clock (default `time.time`).
- [ ] **Step 4: Run** the tests -> PASS; `uv run ruff check src tests` clean.
- [ ] **Step 5: Commit** `feat(progress): reporter events, ETA estimator, timings`.

### Task 2: Renderers, active reporter, stderr cooperation

**Files:**
- Create: `src/repka/progress/render.py`, `src/repka/progress/active.py`
- Modify: `src/repka/logging_setup.py` (stream handler), `src/repka/progress/__init__.py` (exports)
- Test: `tests/test_progress_render.py`

**Interfaces:**
- Consumes: `Sink`, `RunProgress`, `fmt_duration` (Task 1).
- Produces (`render.py`): `class LinesRenderer(stream, clock, wall)`, `class JsonlRenderer(stream, clock, wall)`, `class LiveRenderer(stream, clock, wall, columns: Callable[[], int])`, `class PlainRenderer(write: Callable[[str], None])`, each a `Sink` with `owns_stderr: bool` (True for live and jsonl) and `log_line(text: str, level: str = "INFO") -> None`; `make_renderer(mode: str, stream, *, is_tty: bool, ...) -> Sink` (`live` + not tty -> `LinesRenderer`).
- Produces (`active.py`): `activate(sink_owner: Reporter-with-renderer) -> ContextManager`, `active() -> Active | None`, `stderr_line(text: str, level: str = "INFO") -> None` (renders via the active renderer if it owns stderr, else `print(..., file=sys.stderr, flush=True)`).
- `logging_setup`: replace `logging.StreamHandler(sys.stderr)` with `ActiveStreamHandler` whose `emit` calls `stderr_line` semantics for log records (formatted text) when an active renderer owns stderr, else writes to stderr as today.

- [ ] **Step 1: Write failing tests**: `test_lines_format_and_throttle` (stage change prints immediately, ten `advance` calls within 10 s print one line, line contains `3/5`, stage name, percent, `eta ~`); `test_lines_final_line_on_finish`; `test_jsonl_every_line_is_json` (events, notes, `log_line` and `stderr_line` while activated all parse with `json.loads`; `event` keys as in the spec); `test_jsonl_progress_throttled_to_one_per_second`; `test_live_redraws_single_line_and_prints_finished_stage_permanently` (carriage return and `\x1b[2K` present, finished-stage line ends with `\n`); `test_live_trims_to_columns`; `test_live_log_line_clears_then_redraws`; `test_live_on_non_tty_falls_back_to_lines` (no `\x1b`, no `\r`); `test_live_ascii_bar_when_not_utf8`; `test_printable_applied_to_domain_label` (control characters in a label are stripped); `test_active_stderr_line_plain_goes_to_stderr` (capsys); `test_logging_handler_routes_through_active_jsonl`.
- [ ] **Step 2: Run** -> FAIL.
- [ ] **Step 3: Implement** renderers and `active.py`. `LiveRenderer`: redraw at most 5/s on events, plus `start_ticker()` / `stop()` using a daemon `threading.Timer` loop every 1 s guarded by a lock (tests call `redraw()` directly, never start the thread). Lines format per spec D6; bar width 12.
- [ ] **Step 4: Run** tests -> PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(progress): lines, live and jsonl renderers; shared-stderr handling`.

### Task 3: Status file and `repka status`

**Files:**
- Create: `src/repka/progress/status.py`, `src/repka/status_cli.py`
- Modify: `src/repka/cli.py` (register), `wiki/spec/architecture.md` later (Task 6)
- Test: `tests/test_progress_status.py`

**Interfaces:**
- Consumes: `Sink`, snapshot shape (Task 1), `RunLock` (`src/repka/runlock.py`).
- Produces (`status.py`): `class StatusFileSink(path: Path, pid: int, run_id: int, mode: str, wall)` (a `Sink`; atomic write via temp file + `os.replace`; heartbeat rewrite at most every `HEARTBEAT_S` on `progress` events, always on stage/step/finish events); `read_status(path: Path) -> dict | None` (None on missing/corrupt); `liveness(state_dir: Path) -> Literal["running", "idle"]` (non-blocking `fcntl.flock` on `run.lock`; acquired -> `idle`, and releases at once).
- Produces (`status_cli.py`): `register(sub)` adding `repka status [--json]`; text report: running (stage, step, done/total, elapsed, eta, heartbeat age, warning past `STALE_HEARTBEAT_S`), idle with last run, or "died" when file says `running` but liveness is `idle`; no file -> "no run recorded yet", exit 0.

- [ ] **Step 1: Write failing tests**: `test_write_is_atomic_and_complete` (file always parses; fields `schema`, `pid`, `run_id`, `status`, `updated_at`, `current`, `overall`); `test_heartbeat_throttled`; `test_final_status_written_on_finish`; `test_read_status_corrupt_returns_none`; `test_liveness_busy_when_lock_held` (hold a `RunLock`); `test_status_cli_running` (lock held + file), `test_status_cli_dead_run_reported_not_running` (file `running`, no lock), `test_status_cli_json`, `test_status_cli_no_file`, `test_status_cli_warns_on_stale_heartbeat`.
- [ ] **Step 2: Run** -> FAIL.
- [ ] **Step 3: Implement** per interfaces; command registration follows `doctor_cli.register`.
- [ ] **Step 4: Run** tests -> PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(status): atomic progress.json and repka status`.

### Task 4: Instrument stages (additive hooks) and worker output relay

**Files:**
- Modify: `src/repka/collect.py` (`collect_domains`, `_collect_one`), `src/repka/triage/triage.py` (`triage_domain`), `src/repka/embed/embed.py` (`embed_domain`), `src/repka/triage/deciders.py` (`run_worker`, final warning print)
- Test: `tests/test_progress_hooks.py`

**Interfaces:**
- Consumes: `Reporter` (Task 1), `active()` / `stderr_line` (Task 2).
- Produces: `collect_domains(..., reporter: Reporter | None = None)`: per domain, `step(f"{id} search", "queries", n)` + `advance(i, n)` per query, then `step(f"{id} enrich", "repos", m)` + `advance(done, total)` per batch; existing string `say` messages stay. `triage_domain(..., on_progress: Callable[[int, int, str], None] | None = None)`: `(done, total, detail)`, called with `detail="readme i/n"` while fetching READMEs of a chunk (done unchanged) and `(start + len(items_decided_chunk), total, "")` after each chunk. `embed_domain(..., on_progress: Callable[[int, int, str], None] | None = None)`: after each batch. `run_worker`: when `active()` reports `owns_stderr`, run with `stdout=PIPE, stderr=STDOUT, text=True`, relay each captured line through `stderr_line`; also on `TimeoutExpired` use `exc.output`; the "stopped early" warning goes through `stderr_line(..., "WARNING")`. Otherwise unchanged.

- [ ] **Step 1: Write failing tests**: `test_collect_emits_steps_and_advances` (recording reporter, fake client from `tests/test_collect.py` fixtures; existing string messages still emitted; no reporter -> identical behavior); `test_triage_progress_per_chunk` (fake backend, 450 repos -> done values 200, 400, 450; `readme` details seen); `test_triage_without_callback_unchanged`; `test_embed_progress_per_batch`; `test_run_worker_relays_output_when_stderr_owned` (fake worker script writing two lines; activated jsonl renderer collects two JSON lines); `test_run_worker_inherits_fd_otherwise`.
- [ ] **Step 2: Run** -> FAIL.
- [ ] **Step 3: Implement** the hooks; callbacks are wrapped so an exception in them is swallowed and logged at DEBUG.
- [ ] **Step 4: Run** `uv run pytest -q` (whole suite: existing collect/triage/embed tests must still pass) -> PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(progress): additive progress hooks in collect, triage, embed and worker`.

### Task 5: Wire into `run_pass` and `repka run`

**Files:**
- Modify: `src/repka/pipeline.py` (`run_pass`, `build_boundaries`), `src/repka/run_cli.py`, `src/repka/config.py` (`RunConfig.progress`, `load_run`)
- Test: `tests/test_progress_pipeline.py`, extend `tests/test_config.py`

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: `RunConfig.progress: str = "plain"` (`_choice("plain","live","lines","jsonl")`); `run_pass(..., reporter: Reporter | None = None)` emits `plan`, `stage_start(stage, steps=len(enabled))`, `step(domain.id, unit, total)` per domain for triage/embed, `stage_end` (status `ok`/`error`/`skipped`, `skipped` for disabled/not-due stages, detail = short outcome), and `build_boundaries(..., reporter=None)` passes `on_progress`/`reporter` into the stage functions; `pipeline.report_dict` unchanged. `run_cli`: `--progress {plain,live,lines,jsonl}` (default from config), builds `RunProgress` (history from `SELECT stats_json FROM runs WHERE kind='run' AND status IN ('ok','partial') ORDER BY id DESC LIMIT 20`) with sinks: the chosen renderer (`PlainRenderer(_progress)` keeps the old `progress=` string callback wiring) and `StatusFileSink(state_dir()/"progress.json", ...)`; wraps the pass in `activate(...)`; stores `{**report_dict(..., full=False), "timings": progress.timings()}` in the runs row only; crash net and `finally` call `progress.finish("failed" | report.status)` so the live line is closed; header, lock-busy, crash and `--json` summary prints go through `stderr_line`.

- [ ] **Step 1: Write failing tests**: `test_plain_mode_stderr_is_byte_identical` (run the existing fake-boundaries flow with default config; stderr equals the pre-change lines); `test_run_progress_config_validation` (`run: {progress: fast}` -> `ConfigError` naming `run.progress`); `test_lines_mode_reports_every_stage_in_order` (fake boundaries + fake clock; collect, triage, embed, digest lines present, ordered, final line present); `test_jsonl_mode_every_stderr_line_is_json_and_stdout_untouched` (`--json`: stdout parses and has no `timings`; every stderr line parses); `test_status_file_written_in_default_plain_mode_and_finalised`; `test_runs_row_has_timings_and_old_shape_intact`; `test_stage_error_marks_stage_error_event`; `test_disabled_stages_are_skipped_in_plan`; `test_crash_finalises_live_line_and_status_failed` (boundary raises `KeyboardInterrupt`; status file `failed`, stderr ends with newline); `test_history_feeds_overall_eta` (seed `runs` rows with `timings`, first `lines` output of a fake pass shows `eta`).
- [ ] **Step 2: Run** -> FAIL.
- [ ] **Step 3: Implement** the wiring; keep `run_pass`'s positional signature and `progress` parameter.
- [ ] **Step 4: Run** `uv run pytest -q` (entire suite incl. `test_pipeline.py`, `test_e2e.py`) -> PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(run): progress modes, status file and timings wired into repka run`.

### Task 6: Docs, rehearsal, final verification

**Files:**
- Modify: `wiki/spec/architecture.md` (§3.2 `run.progress`; §5 progress/status paragraph; §7 table rows `repka status`, `repka run --progress`), `README.md` (section "Watching a run"), `wiki/plans/ROADMAP.md` (task-board row), `.config.yaml` untouched.
- Test: `tests/test_e2e.py` (add one `lines`-mode pass), full suite.

- [ ] **Step 1: Add** the e2e test `test_e2e_lines_mode_pass` driving a whole pass against the existing fixtures with `--progress lines`; assert stage lines and final line.
- [ ] **Step 2: Rehearse** in a scratch home: `REPKA_HOME=$(mktemp -d)` then `uv run repka run --progress lines`, `--progress jsonl | jq -c .` on stderr, and `repka status` from a second shell while a fake long stage sleeps (or a scripted `python -c` driving `RunProgress` with real clock); record what was observed.
- [ ] **Step 3: Update docs** as listed; mention D1's open question in README only as "default is `plain`".
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests` -> all green; verify `git diff main --stat` touches only expected files.
- [ ] **Step 5: Commit** `docs(run): observability modes, status command, architecture`.
