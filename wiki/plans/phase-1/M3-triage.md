# M3: Triage with decider models

[← ROADMAP](../ROADMAP.md) · Prev: [M2](M2-collector.md) · Next: [M4 Embeddings](M4-embeddings.md)

Goal of the milestone: every repo linked to a domain gets a verdict from a local decider ("relevant?", "substance level", "kind"), stored in `verdicts`, and `accepted` repos are what the digest and embeddings use.

Research: [deciders](../../research/deciders.md) · [reference map](../../research/reference-tegerun.md) · ADR: [ADR-003](../../spec/decisions.md#adr-003-local-decider-backends-via-worker-subprocesses), [ADR-004](../../spec/decisions.md#adr-004-jev-like-means-a-local-decider-hosted-jev-is-optional-and-off) · Spike result: `wiki/spikes/T03-deciders.md`

---

### P1-T14: Port the decider runtime

**Req:** R-1.3 · **Depends on:** T03 (the `deciders/` kit and spike verdict) · **Size:** M
**Goal:** A Python-side runtime that loads questions/models, builds worker requests, runs the worker subprocess safely and normalizes answers — ported from `as.tegerun.cli/src/tegerun/classify.py`.

**Files:**
- Create: `src/repka/triage/__init__.py`, `src/repka/triage/deciders.py`
- Test: `tests/test_deciders.py`, `tests/fixtures/deciders/fake_worker.py`, `tests/fixtures/deciders/out_d1.json` (a real `out.json` captured during the T03 spike, trimmed to 3 results)

**Interfaces:**
- Consumes: `TriageConfig`, `config.deciders_dir()`, `config.worker_env()`, `ConfigError`, `models.TriageItem`.
- Produces (`repka.triage.deciders`):
  - `class WorkerError(RuntimeError)`.
  - `@dataclass(frozen=True) QuestionSet(questions: dict[str, dict], state_template: str)` · `load_questions(path: Path) -> QuestionSet` (enabled questions only, file order kept) · `merge_questions(base: QuestionSet, overrides: dict) -> QuestionSet` (domain `triage.questions`: same id replaces, new id appends; `enabled: false` removes) · `questions_hash(q: QuestionSet) -> str` (sha256 of canonical JSON of questions + template, first 16 hex).
  - `@dataclass(frozen=True) ModelSpec(name: str, runner: str, venv: str, repo: str, revision: str, grouping: Literal["joint","single"], device: str, dtype: str, remote: bool = False)` · `load_models(path: Path) -> dict[str, ModelSpec]` (rejects any revision that isn't 40 hex chars).
  - `build_requests(items: Sequence[TriageItem], q: QuestionSet, grouping: str) -> list[dict]` — `rid = f"{item.domain_id}:{item.repo_id}"`; `single` → one request per question with `rid = f"{rid}#{qid}"`.
  - `run_worker(spec: ModelSpec, requests: list[dict], *, deciders_dir: Path, timeout_s: int, online: bool) -> dict` — writes `requests.json` in a fresh temp dir (also the cwd), runs `deciders_dir/spec.venv/bin/python -I deciders_dir/workers/<runner>.py --config <json> --requests ... --out ...` with `env=worker_env()`, worker stdout/stderr streamed to our stderr; returns parsed `out.json`; a crash/timeout with **no** `out.json` → `WorkerError`; a crash **with** partial `out.json` → returns it (log warning).
  - `normalize_answer(question: dict, raw: dict) -> dict` — identical semantics to the reference (`noul → {type, p, pred, conf}`, `choice → {type, pred, conf, probs}`, `score → {type, pred, score, conf, probs}`).
  - `class DeciderBackend(Protocol)`: `decide(items: Sequence[TriageItem], q: QuestionSet) -> tuple[dict[str, dict[str, dict]], dict[str, str]]` returning `(answers_by_rid, errors_by_rid)`, answers normalized and re-joined per item for `single`.
  - `class LocalWorkerBackend(DeciderBackend)` (`spec`, `deciders_dir`, `timeout_s`, `online`).
  - `make_backend(cfg: TriageConfig, models: dict[str, ModelSpec]) -> DeciderBackend` — unknown model → `ConfigError`; `spec.remote and not cfg.allow_remote` → `ConfigError("remote decider backends are disabled (triage.allow_remote: false)")`.

- [ ] **Step 1: Write failing tests**

```python
def test_revision_must_be_40_hex(tmp_path)
def test_load_questions_skips_disabled()
def test_merge_questions_override_add_remove()
def test_questions_hash_changes_with_instructions()
def test_build_requests_joint_vs_single()
def test_normalize_noul_choice_score()               # values from out_d1.json
def test_run_worker_scratch_cwd_and_env_without_secrets(tmp_path, monkeypatch)  # fake_worker.py dumps os.getcwd() and env into out.json
def test_partial_out_json_used_on_crash()            # fake worker writes 1 result then exits 1
def test_timeout_without_output_raises_worker_error()
def test_remote_backend_refused_unless_allowed()
def test_single_grouping_rejoins_answers_per_item()
```

`fake_worker.py` follows the CLI contract (`--config --requests --out`) and its behaviour is controlled by a field in `--config` (`{"mode": "ok" | "crash_after_1" | "sleep"}`); tests point `deciders_dir` at a tmp dir with `venv/bin/python` symlinked to `sys.executable`.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** by porting the reference functions; keep its comments on failure blame. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit & push**, report. Message: `feat(triage): decider runtime ported from tegerun`.

**Acceptance criteria:** the main package imports no torch/transformers/mlx (`test_smoke` asserts `"torch" not in sys.modules` after importing all `repka` modules); secrets never reach the worker (test).

---

### P1-T15: Repository triage and `repka triage`

**Req:** R-1.3 · **Depends on:** T05, T09, T10, T14 · **Size:** M
**Goal:** Decide pending repos of a domain, store verdicts, compute `accepted`.

**Files:**
- Create: `src/repka/triage/triage.py`, `src/repka/triage/cli.py`
- Modify: `src/repka/cli.py`
- Test: `tests/test_triage.py`

**Interfaces:**
- Consumes: `DeciderBackend`, `QuestionSet`, `load_questions`, `merge_questions`, `questions_hash`, `load_models`, `make_backend`, `WorkerError`; `Domain`; store `repos_for_domain`; `GitHubClient.readme`; `textutil.printable`, `truncate`.
- Produces (`repka.triage.triage`):
  - `render_state(template: str, fields: dict[str, str]) -> str` — single-pass substitution of `{name}` placeholders (values containing `{...}` are not expanded again).
  - `meta_hash(row) -> str` — sha256 of `full_name|description|topics_json|language` (stars excluded so star growth doesn't re-trigger triage).
  - `pending_repos(conn, domain_id: str, model: str, qhash: str) -> list[sqlite3.Row]` — repos linked to the domain, `status='active'`, with no verdict for `(model, qhash)` or a verdict whose `input_hash != meta_hash(row)`.
  - `build_item(row, readme: str, domain: Domain, readme_chars: int, template: str) -> TriageItem` (all fields through `printable`; README truncated; `template` = `QuestionSet.state_template`, needed to render the state: added to the original signature; `stars` comes from the latest snapshot, which `pending_repos` joins in).
  - `store_readme(conn, repo_id: int, excerpt: str, now: str) -> None` — saves the truncated README into `repos.readme_excerpt`/`readme_fetched_at` so embeddings (T18) need no network; a stored excerpt younger than 7 days is reused instead of refetching.
  - `validate_thresholds(thresholds: dict[str, float], q: QuestionSet) -> None` — each key must be an enabled `noul` or `score` question, else `ConfigError`; empty map → `ConfigError`.
  - `is_accepted(answers: dict[str, dict], thresholds: dict[str, float]) -> bool` — noul: `p >= t`; score: `pred >= t`; a missing answer → `False`.
  - `fresh_excerpt(row, now) -> str | None` (the stored README excerpt if `readme_fetched_at` is under 7 days old; an empty README counts as fetched).
  - `@dataclass TriageStats(pending: int = 0, decided: int = 0, accepted: int = 0, errors: int = 0, rate_limited: bool = False, decisions: list[tuple[str, dict, bool]] = [])` (the last two are additions: the CLI table/`--json` and the rate-limit warning need them).
  - `triage_domain(conn, domain: Domain, backend: DeciderBackend, q: QuestionSet, model: str, thresholds: dict[str, float], readme_fn: Callable[[str], str], readme_chars: int, now: str, limit: int | None = None) -> TriageStats` — one `backend.decide` call for all pending items of the domain (chunks of 200 to bound memory); README from a fresh stored excerpt, else `readme_fn`; if `readme_fn` raises `RateLimited`, that repo and all later ones needing a fetch stay pending this run (no further fetches), repos with stored excerpts are still decided (RF9); per-item errors → no verdict row; `WorkerError` propagates after nothing is written for that chunk. Fetched READMEs are committed before `decide`, so a worker failure does not waste REST budget; a non-rate-limit `GitHubError` on a README fetch counts as a per-item error (repo stays pending).
  - CLI `repka triage [--domain ID] [--limit N] [--dry-run] [--json]`: runs explicitly even when `triage.enabled` is false (the flag governs `repka run`); the CLI stops at the first `WorkerError` (`error:` + exit 1; run-level RF7 handling belongs to T22); `--domain` runs a disabled domain when named explicitly; `--json` prints `{dry_run, domains: [...]}`; `--dry-run` prints the pending count and the first rendered state and loads no model; output table `repo · relevant p · substance · kind · accepted`.

- [ ] **Step 1: Write failing tests** (`FakeBackend` returns canned normalized answers or errors):

```python
def test_render_state_does_not_expand_placeholders_in_values()
def test_pending_skips_up_to_date_verdicts_and_retriggers_on_description_change()
def test_stars_change_does_not_retrigger()
def test_validate_thresholds_rejects_choice_and_unknown()
def test_is_accepted_all_thresholds_must_pass()
def test_triage_writes_verdicts_and_accepts()
def test_worker_failure_leaves_items_pending()          # RF7: backend raises WorkerError -> no verdict rows; per-item error -> that item stays pending
def test_readme_fetched_only_for_pending_and_stored()
def test_rate_limited_readme_leaves_item_pending()   # RF9
def test_triage_cli_dry_run_loads_no_model(home, monkeypatch)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Manual run** after a T13 collect: `uv run repka triage --domain <id> --limit 20`; paste the table into the report with a short judgement of quality. If quality is poor, tune `deciders/questions.yaml` wording / thresholds and note it.
- [ ] **Step 6: Commit & push**, report. Message: `feat(triage): repository triage and repka triage`.

**Acceptance criteria:** verdicts are idempotent (second run decides nothing new); thresholds validated at startup; a worker crash never marks a repo accepted or rejected.

---

### P1-T16: (Optional) Clef MLX worker

**Req:** R-1.3, R-G2 · **Depends on:** T14; **only if** the T03 spike recorded `Clef: GO` (otherwise set status `dropped`) · **Size:** M
**Goal:** Add Clef(-flash) on MLX as a second local backend with the identical worker contract.

**Files:**
- Create: `deciders/workers/clef_mlx_worker.py`, `tests/fixtures/deciders/out_clef.json`
- Modify: `deciders/setup.sh` (create `.venv-mlx` with the packages the model card requires), `deciders/models.yaml` (entry `clef-flash-mlx`, runner `clef_mlx_worker`, venv `.venv-mlx`, repo + **full pinned revision** from the spike), `deciders/README.md`
- Test: `tests/test_deciders.py` (add `test_normalize_clef_out_json`)

**Interfaces:**
- Consumes: worker contract ([deciders § 3](../../research/deciders.md#3-integration-pattern-reused-from-the-reference-project)).
- Produces: `triage.model: clef-flash-mlx` usable in `.config.yaml`; raw answers in the same shape as d1 (`noul`, `choice`+`confidence`+`probabilities`, `score`+`confidence`+`probabilities`). If Clef's native output differs, the worker converts it.

- [ ] **Step 1:** Review the model's loader code at the pinned revision (`./deciders/setup.sh --code-only clef-flash-mlx`); note in the report what remote code runs.
- [ ] **Step 2: Write the worker** mirroring `d1_worker.py` structure (offline by default, saves `out.json` every 10 requests, prints load time).
- [ ] **Step 3: Capture** `out_clef.json` from a run on the T03 sample; **write test** `test_normalize_clef_out_json` → PASS.
- [ ] **Step 4: Compare** with d1 on the sample (accuracy, ms/item, RAM) and append to `wiki/spikes/T03-deciders.md`.
- [ ] **Step 5: Commit & push**, report. Message: `feat(deciders): optional Clef MLX backend`.

**Acceptance criteria:** `uv run repka triage --domain <id> --limit 5` works with `triage.model: clef-flash-mlx`; comparison recorded.
