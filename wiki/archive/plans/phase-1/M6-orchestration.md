# M6: Orchestration, daemon, hardening

[← ROADMAP](../ROADMAP.md) · Prev: [M5](M5-digest.md) · Next: [Gate G1](../ROADMAP.md#gate-g1-phase-1-is-stable) → [M7 Dashboard](../phase-2/M7-dashboard.md)

Goal of the milestone: `repka run` performs one full, safe pass (collect → triage → embed → digest); launchd runs it hourly; `repka doctor` tells a new user exactly what is missing; an end-to-end test and README prove it all fits together.

Contract: [architecture § 1 and § 5](../../../spec/architecture.md#5-pipeline-semantics) · ADR: [ADR-006](../../../spec/decisions.md#adr-006-scheduling--hourly-launchd-pass--slot-logic-inside-the-app)

---

### P1-T22: `repka run` pipeline

**Req:** R-1.6, R-G1 · **Depends on:** T13, T15, T18, T21 · **Size:** M
**Goal:** Pure orchestration with injected boundaries (reference `run_pass` pattern), a lock, run records, and partial-failure tolerance.

**Files:**
- Create: `src/repka/pipeline.py`, `src/repka/run_cli.py`
- Modify: `src/repka/cli.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `collect_domains` / `CollectReport`, `due_domains`; `triage_domain` / `TriageStats`, `make_backend`, `WorkerError`; `embed_domain` / `EmbedStats`, `OllamaEmbedder`, `EmbedderUnavailable`; `run_digest` / `DigestOutcome`; `TelegramUserSender`; `RunLock`, `LockBusy`; store `start_run`, `finish_run`, `migrate`.
- Produces (`repka.pipeline`):
  - `@dataclass Boundaries(collect: Callable[[list[Domain], datetime], CollectReport], triage: Callable[[Domain, datetime], TriageStats], embed: Callable[[Domain, datetime], EmbedStats], digest: Callable[[datetime, bool], DigestOutcome])`.
  - `@dataclass PassReport(status: Literal["ok","partial","failed"], collect: CollectReport | None, triage: dict[str, TriageStats], embed: dict[str, EmbedStats], digest: DigestOutcome | None, errors: list[str])`.
  - `run_pass(domains: list[Domain], cfg: AppConfig, b: Boundaries, now: datetime, send: bool) -> PassReport` — order: collect (due domains only) → triage (every enabled domain, only if `cfg.triage.enabled`) → embed (only if `cfg.embed.enabled`) → digest (always evaluated; sends only if `send`). Each stage's exception is caught, logged, appended to `errors`, and the pass continues; `RateLimited`, `WorkerError`, `EmbedderUnavailable` → `partial`; an unexpected exception in every stage → `failed`.
  - `explain(report: PassReport, cfg: AppConfig) -> list[str]` — hints such as "triage is disabled (triage.enabled: false), so nothing is accepted and digests stay empty", "no enabled domains", "digest preview only: set notify.enabled and run.send".
  - `build_boundaries(conn, cfg: AppConfig) -> Boundaries` — real wiring; constructs the GitHub client / decider backend / embedder / sender lazily, only when the stage runs.
  - CLI `repka run [--send] [--domain ID] [--json]`: migrate DB → (T24 adds preflight here) → `RunLock(state_dir()/"run.lock")` (`LockBusy` → message on stderr, exit 0) → `start_run` → `run_pass` → `finish_run` → summary line on stderr, `--json` report on stdout. `send = cfg.run.send or --send`; `send` with `notify.enabled: false` → `ConfigError` (exit 2). Exit code 0 for `ok`/`partial`, 1 for `failed`.

- [ ] **Step 1: Write failing tests** (fake boundaries recording call order):

```python
def test_stage_order_and_flags_off_skip_triage_and_embed()
def test_rate_limited_run_is_partial_and_continues_to_digest()   # RF1
def test_worker_error_is_partial_and_continues()                 # RF7
def test_embedder_unavailable_is_partial()
def test_send_requires_notify_enabled(home)                      # exit 2
def test_lock_busy_exits_zero(home)
def test_runs_table_records_status_and_stats(home)
def test_domain_filter_limits_all_stages()
def test_explain_mentions_disabled_triage()
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit & push**, report. Message: `feat: repka run pipeline`.

**Acceptance criteria:** a failure in one stage never prevents the digest stage; two concurrent `repka run` never both work; every pass is recorded in `runs`.

**As built (deviations, P1-T22):** (1) `build_boundaries(conn, cfg, domains, *, force=False, progress=None)`: the domain list is needed by the digest and `force` (`--domain`) by collect; the due-domain filter lives in the real collect boundary (it reads the domain states and builds no GitHub client when nothing is due), because `run_pass` has no connection. (2) `Boundaries` has a defaulted `close()` (closes the lazily built client and embedder); `PassReport` has defaulted `domains` and `send` (used by `explain`); `run_pass` takes an optional `progress` callback. (3) Status rules: `partial` also for collect domains reported `partial`/`skipped` (collect reports a rate limit, it does not raise), triage per-item errors / rate-limited README fetches, and a failed digest send; `failed` only when every stage that ran raised an unexpected exception. Triage and embed stop at their first raised exception (a broken worker/Ollama would fail every domain). (4) Output: without `--json` the summary (with `errors:`/`hints:` and, when not sending, the digest preview text) goes to stdout; with `--json` the full report goes to stdout and one summary line to stderr. (5) `--domain` for a disabled domain is a `ConfigError` (as in `collect`). (6) Lock first, then migrate (same as `collect`). (7) The T13 silent-baseline fix: `enrich_repos(..., baseline=True)` suppresses `release` events for first-sighting repos during a domain's first scan (see architecture § 5); no migration of old events.

---

### P1-T23: launchd schedule

**Req:** R-1.6, R-G2 · **Depends on:** T22 · **Size:** S
**Goal:** `repka schedule install|uninstall|status` manages one hourly launchd user agent.

**Files:**
- Create: `src/repka/schedule.py`
- Modify: `src/repka/cli.py`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Consumes: `project_root()`, `logs_dir()`.
- Produces (`repka.schedule`):
  - `LABEL = "com.repka.run"`; `plist_path() -> Path` (`~/Library/LaunchAgents/com.repka.run.plist`).
  - `plist_xml(uv_path: str, root: Path, interval_s: int = 3600) -> str` — `ProgramArguments = [uv_path, "run", "--project", str(root), "repka", "run"]`, `WorkingDirectory = root`, `StartInterval = interval_s`, `RunAtLoad = false`, `StandardOutPath`/`StandardErrorPath` = `logs/launchd.log`; **no secrets and no environment variables** in the plist.
  - `install(run_cmd: Callable[[list[str]], CompletedProcess] = subprocess.run, dry_run: bool = False) -> str`, `uninstall(run_cmd=...) -> str`, `status(run_cmd=...) -> str` using `launchctl bootstrap gui/<uid> <plist>`, `launchctl bootout gui/<uid>/<LABEL>`, `launchctl print gui/<uid>/<LABEL>` (verify flags on this macOS version and note it).
  - CLI `repka schedule install [--dry-run] [--interval 1h]`, `uninstall`, `status`. `install` warns (does not refuse) when `notify.enabled` or `run.send` is false: "runs will collect and triage but send no digests". Re-install replaces the old agent (bootout first).

- [ ] **Step 1: Write failing tests** (fake `run_cmd`, `HOME` → tmp):

```python
def test_plist_is_valid_and_has_no_secrets()   # plistlib.loads; no "TOKEN" anywhere
def test_install_writes_plist_and_bootstraps()
def test_reinstall_boots_out_first()
def test_uninstall_removes_plist()
def test_dry_run_prints_plist_and_runs_nothing()
def test_install_warns_when_sending_disabled(capsys)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Manual check (human approval needed — modifies the user's LaunchAgents):** `uv run repka schedule install`, `uv run repka schedule status`, wait one interval or `launchctl kickstart gui/$UID/com.repka.run`, check `logs/launchd.log`, then `uninstall` if this was only a test.
- [ ] **Step 6: Commit & push**, report. Message: `feat: launchd schedule commands`.

**Acceptance criteria:** install/uninstall are idempotent; nothing is installed without an explicit command.

---

### P1-T24: Preflight and `doctor`

**Req:** R-1.6 · **Depends on:** T22 · **Size:** M
**Goal:** Before `run`, and on `repka doctor`, list every missing piece at once with the exact command to fix it (reference `preflight.py` UX). Preflight only reads files; no network, no model loading.

**Files:**
- Create: `src/repka/preflight.py`
- Modify: `src/repka/run_cli.py` (call preflight before the lock), `src/repka/cli.py` (`doctor`)
- Test: `tests/test_preflight.py`

**Interfaces:**
- Consumes: `load_config`, `load_domains`, `load_models`, `load_questions`, `validate_thresholds`, `config.deciders_dir()`, `config.github_token()`, `config.github_token_source()`, `GitHubClient.check_auth` / `AuthInfo` / `AuthError` (T10), Telegram credentials (presence only).
- Produces (`repka.preflight`):
  - `@dataclass(frozen=True) Finding(level: Literal["error","warning"], title: str, fix: str)`.
  - `check(cfg: AppConfig, *, send: bool, auth_probe: Callable[[], AuthInfo] | None = None, now: datetime | None = None) -> list[Finding]` (default probe = a real `GitHubClient(...).check_auth()`; tests always inject a fake), checking (error unless noted):
    1. a GitHub token is available (`REPKA_GITHUB_TOKEN` in `.env` or `gh auth token` succeeds) → fix `gh auth login` or put a token in `.env`.
    1b. only if a token exists: the token works — `auth_probe()` raising `AuthError` → error "GitHub token expired, revoked or invalid" with the source-specific fix (T10 wording); `expires_at` within 7 days of `now` → warning "GitHub token expires on <date>" with the same fix; network/transport failure → warning "could not verify the GitHub token (offline?)" and continue.
    2. at least one enabled, valid domain → fix `uv run repka domain new` or set `enabled: true`.
    3. if `triage.enabled`: model in `models.yaml`; its venv exists (`deciders/<venv>/bin/python`); pinned snapshot in the HF cache holds a weight file → fixes `./deciders/setup.sh`, `./deciders/setup.sh --download <model>`; thresholds valid.
    4. if `embed.enabled`: `ollama` binary on `PATH` (warning; reachability is checked at run time).
    5. if `send`: `REPKA_TG_API_ID`/`REPKA_TG_API_HASH` set, session file exists (fix `uv run repka notify login`), `notify.chat_id` non-empty.
    6. warning when `triage.enabled` is false ("digests will stay empty") or sending is off ("preview only").
  - `format_findings(findings: list[Finding]) -> str` — numbered to-do list, errors first.
  - `run` exits 2 with the list when any error exists; warnings print and continue. `repka doctor` prints the list (or "all good") and exits 2 on errors, else 0.

- [ ] **Step 1: Write failing tests** (one per check, plus ordering and exit codes):

```python
def test_no_github_token_is_error_with_fix(home, monkeypatch)
def test_rejected_token_is_error_with_fix(home)        # fake probe raises AuthError
def test_token_expiring_soon_is_warning(home)          # expires_at = now + 3d
def test_probe_network_failure_is_warning(home)
def test_probe_skipped_when_no_token(home, monkeypatch) # only finding 1, probe not called
def test_no_enabled_domain_is_error(home)
def test_triage_missing_venv_and_weights(home, tmp_path)   # fake HF cache dir structure
def test_code_only_snapshot_counts_as_missing_weights(home)
def test_send_without_tg_credentials_session_or_chat(home)
def test_flags_off_produce_warnings_only(home)
def test_doctor_exit_codes(home)
def test_run_stops_on_errors_before_lock(home)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. Tests that drive `repka run` elsewhere stub `preflight.check`.
- [ ] **Step 5: Commit & push**, report. Message: `feat: preflight and repka doctor`.

**Acceptance criteria:** on a fresh clone `uv run repka doctor` prints a complete numbered to-do list; an expired or revoked GitHub token is reported by `repka doctor` and stops `repka run` before any search; the only network call in preflight is one `GET /user`, and no model is imported.

**As built (deviations, P1-T24):** (1) `preflight.diagnose(cfg, *, send, auth_probe, now) -> list[Result]` returns every check including the ok ones (`Result(id, status ok|warn|error, title, fix)`); `check()` is the plan's signature and returns only the problems as `Finding`s (errors first) via `findings_of`. `format_results` prints the one-line-per-check view. (2) `repka doctor` lives in `src/repka/doctor_cli.py` (registered in `cli.py`) and adds `--send` and `--json`; it prints the per-check lines followed by the numbered to-do list when there are problems. (3) `repka run` calls `preflight.check` after domain selection and before the lock; on errors it prints the list on stderr, `error: repka run did not start...` and exits 2; warnings are printed and the run continues. (4) New checks beyond the plan: `triage.allow_remote` for a remote model, and `send` requested while `notify.enabled` is false. With `triage.online: true` missing weights are a warning. (5) `github.client.FIX_ENV` / `FIX_GH` are now public (the preflight reuses the T10 fix wording); `config.session_file_path()` is a non-creating variant of `session_file()` (preflight never creates `.sessions/`). (6) Existing CLI tests in `tests/test_pipeline.py` stub `preflight.check` with an autouse fixture.

---

### P1-T25: End-to-end rehearsal and README

**Req:** R-1.6, R-G1 · **Depends on:** T23, T24 · **Size:** M
**Goal:** Prove the whole Phase 1 pipeline works together (offline test) and on real services (human-assisted rehearsal); document it.

**Files:**
- Create: `tests/test_e2e.py`, `tests/fixtures/e2e/` (search pages, graphql responses, decider answers), `wiki/spikes/T25-rehearsal.md`
- Modify: `README.md` (full user guide), `CLAUDE.md` (architecture summary, commands), `wiki/spec/architecture.md` (any drift found)

**Interfaces:**
- Consumes: everything above, through real modules: `GitHubClient` on `httpx.MockTransport` serving fixtures, `LocalWorkerBackend` replaced by a fake backend, `FakeEmbedder`, a fake `Sender` recording parts.

- [ ] **Step 1: Write `tests/test_e2e.py`:**

```python
def test_full_pass_produces_one_digest(home):
    # domain enabled, triage+embed enabled, notify enabled, send=True, now = 08:45 local with explicit tz=ZoneInfo("Asia/Tbilisi")
    # pass 0 (previous day): first scan = silent baseline, nothing in the digest
    # pass 1: incremental scan finds 3 new repos, 2 accepted / 1 rejected, embeddings for 2, digest sent with the 2 accepted (rejected absent)
def test_second_pass_same_slot_sends_nothing(home)
def test_next_slot_reports_only_new_events(home)   # clock 19:45, a new release fixture -> digest with that release only
def test_rate_limit_mid_collect_still_sends_digest_of_known_items(home)
```

- [ ] **Step 2: Run** → make it pass, fixing integration bugs in the owning modules (note each fix in the report).
- [ ] **Step 3: Write README.md:** what repka is; requirements (macOS Apple Silicon, uv, GitHub PAT, optional Ollama, ~6 GB for the decider); quick start (`uv sync` → `repka doctor` → follow the list); domains (`domain new`, the Claude skill, `domain plan`); config reference (link to architecture § 3); turning on triage/embeddings/sending; schedule; troubleshooting (rate limits, worker errors, Ollama down, empty digests).
- [ ] **Step 4: Live rehearsal (human-assisted)** — record in `wiki/spikes/T25-rehearsal.md`: real token, one domain with `first_window: 30d`, triage + embed enabled, bot configured; run `repka run --send` around a slot; confirm the Telegram digest, then run again and confirm no duplicate; durations and GitHub calls used.
- [ ] **Step 5: Run** full suite + ruff → PASS. **Commit & push**, report. Message: `test: end-to-end pipeline; docs: README`.

**Acceptance criteria:** offline E2E tests pass; rehearsal page shows a real digest delivered once; README lets a new user reach a first preview digest using only `repka doctor`'s instructions. After this task, the human starts the 7-day [Gate G1](../ROADMAP.md#gate-g1-phase-1-is-stable).
