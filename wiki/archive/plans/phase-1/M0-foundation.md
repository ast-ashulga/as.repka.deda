# M0: Foundation & go/no-go spikes

[← ROADMAP](../ROADMAP.md) · [Agent guide](../AGENT-GUIDE.md) · Next: [M1 Domain Profile](M1-domain-profile.md)

Goal of the milestone: a runnable, tested `repka` skeleton with strict config, plus three spikes that settle the platform risks (vector extension, local decider, embedder) before code depends on them. Each spike ends with a written **GO / NO-GO** in `wiki/spikes/` and a named fallback.

---

### P1-T01: Project scaffold, config and agent guide

**Req:** R-G1, R-G2 · **Depends on:** — · **Size:** M
**Goal:** `uv run repka --help` works; config loads strictly with all flags `false`; shared utilities exist; agents have a `CLAUDE.md`.

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.config.yaml`, `CLAUDE.md`, `src/repka/__init__.py`, `src/repka/cli.py`, `src/repka/config.py`, `src/repka/textutil.py`, `src/repka/runlock.py`, `src/repka/models.py`, `src/repka/logging_setup.py`, `domains/.gitkeep`
- Modify: `.gitignore` (already holds `.env`, `.env.*`, `!.env.example`, `.sessions/`, `*.session` — keep those; add `.state/`, `logs/`, `deciders/.venv-*/`, `!uv.lock`), `README.md` (install + `--help` only)
- Test: `tests/conftest.py`, `tests/test_smoke.py`, `tests/test_config.py`, `tests/test_textutil.py`, `tests/test_runlock.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: nothing. Port patterns (config loader, `RunLock`, text utilities, CLI wiring) from the earlier private prototype.
- Produces:
  - `repka.config`: `class ConfigError(ValueError)`; `project_root() -> Path` (honours `$REPKA_HOME`); `state_dir() / db_path() / logs_dir() / domains_dir() / deciders_dir() -> Path`; `parse_duration(s: str) -> timedelta` (`"30m"`, `"12h"`, `"7d"`); dataclasses `GithubConfig, CollectConfig, TriageConfig, EmbedConfig, DigestConfig, NotifyConfig, RunConfig` and `AppConfig` holding one of each, keys and defaults exactly as [architecture § 3.2](../../../spec/architecture.md#32-configyaml-tracked-every-key-optional-defaults-shown-all-booleans-default-false); `load_config(path: Path | None = None) -> AppConfig`; `read_env() -> dict[str, str]` (reads `.env` via python-dotenv without exporting to `os.environ`; missing file → `{}`); `github_token() -> str` (`REPKA_GITHUB_TOKEN` from `.env`; if empty, falls back to `gh auth token` via `subprocess.run(timeout=10)`; neither → `ConfigError` naming both options; the token is never logged); `github_token_source() -> Literal["env", "gh"]` (which of the two `github_token()` used; for error messages). Telegram credentials are added by T20; `worker_env() -> dict[str, str]` (allowlist: `PATH, HOME, LANG, LC_ALL, TMPDIR, HF_HOME, HF_HUB_CACHE`; never tokens).
  - `repka.textutil`: `printable(s: str) -> str` (drop C0/C1 control chars except `\n\t`), `html_escape(s: str) -> str` (`& < >` and `"`), `truncate(s: str, n: int) -> str` (adds `…`).
  - `repka.runlock`: `class LockBusy(RuntimeError)`; `RunLock(path: Path)` context manager using `fcntl.flock(LOCK_EX | LOCK_NB)`.
  - `repka.models`: the frozen dataclasses of [architecture § 6](../../../spec/architecture.md#6-core-types-srcrepkamodelspy) (fields only; later tasks may add fields with defaults).
  - `repka.cli`: `main(argv: list[str] | None = None) -> int`; `build_parser() -> argparse.ArgumentParser` with a subparser registry later tasks extend: `def register(sub: argparse._SubParsersAction) -> None` functions imported in `build_parser`. Errors: `ConfigError`/`ValueError` → `error: ...` on stderr, exit 2.
  - `repka.logging_setup`: `setup_logging(level: str = "INFO", verbose: bool = False) -> None` (stderr + rotating `logs/repka.log`, 5 × 1 MB).

- [ ] **Step 1: Write `pyproject.toml`** — name `repka`, `requires-python = ">=3.12"`, deps `httpx>=0.27`, `pyyaml>=6`, `python-dotenv>=1.0`, `numpy>=1.26`; script `repka = "repka.cli:main"`; dev group `pytest>=8`, `ruff>=0.6`; hatchling wheel `packages = ["src/repka"]`; `[tool.ruff.lint] select = ["F"]`; pytest `testpaths = ["tests"]`, `markers = ["live: needs network/models; run with REPKA_LIVE=1"]`. Run `uv sync`.
- [ ] **Step 2: Write failing tests**

```python
# tests/conftest.py — fixture `home(tmp_path, monkeypatch)` sets REPKA_HOME=tmp_path and returns it.
# tests/test_config.py
def test_defaults_all_flags_false(home):
    cfg = load_config(home / "missing.yaml")          # missing file -> all defaults
    for section in dataclasses.fields(cfg):
        obj = getattr(cfg, section.name)
        for f in dataclasses.fields(obj):
            if isinstance(getattr(obj, f.name), bool):
                assert getattr(obj, f.name) is False, f"{section.name}.{f.name}"
def test_unknown_key_rejected(home): # {"github": {"per_pag": 1}} -> ConfigError mentioning "github.per_pag"
def test_bool_must_be_yaml_bool(home): # notify: {enabled: "yes"} -> ConfigError
def test_parse_duration():  # "30m"->30 min, "12h", "7d"; "5x" and "" -> ConfigError
def test_project_root_env_override(home): assert project_root() == home
def test_worker_env_has_no_secrets(monkeypatch): # set REPKA_GITHUB_TOKEN/REPKA_TG_API_ID/REPKA_TG_API_HASH -> not in worker_env().values() or keys
def test_tracked_config_is_valid(): load_config(REPO_ROOT / ".config.yaml")  # the shipped example loads
def test_github_token_from_env_wins(home, monkeypatch)          # gh never called
def test_github_token_falls_back_to_gh(home, monkeypatch)      # fake subprocess.run returns the token
def test_github_token_missing_is_config_error(home, monkeypatch) # no .env value, gh missing/failing -> ConfigError
def test_github_token_source(home, monkeypatch)                 # "env" when .env has a value, else "gh"
# tests/test_textutil.py
def test_printable_strips_control(): assert printable("a\x1b[31mb\x07") == "a[31mb"
def test_html_escape(): assert html_escape('<a href="x">&</a>') == "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;"
# tests/test_runlock.py
def test_second_lock_raises_lock_busy(tmp_path)
# tests/test_cli.py
def test_help_exits_zero(); def test_config_error_exits_2(home)  # malformed .config.yaml -> 2, "error:" on stderr
# tests/test_smoke.py — compile every .py under src/, tests/, deciders/workers/ (if exists); import every repka module
```

- [ ] **Step 3: Run** `uv run pytest -q` → FAIL (modules missing).
- [ ] **Step 4: Implement** the modules listed in Produces. Strict loader: each section has `load_<section>(raw: dict) -> <Section>Config`; reject unknown keys and wrong types with the dotted key path in the message.
- [ ] **Step 5: Write `.config.yaml`** (all sections with defaults, every flag `false`, comments explaining each key) and `.env.example` (`REPKA_GITHUB_TOKEN=` with a comment "optional: empty = use `gh auth token`", `REPKA_TG_API_ID=`, `REPKA_TG_API_HASH=`).
- [ ] **Step 6: Write `CLAUDE.md`** for the codebase: commands (`uv sync`, `uv run pytest`, `uv run ruff check src tests`, `uv run repka ...`), architecture pointer to `wiki/spec/architecture.md`, plan pointer to `wiki/plans/ROADMAP.md`, and the Global Constraints (copy the list).
- [ ] **Step 7: Run** `uv run pytest -q && uv run ruff check src tests && uv run repka --help` → all pass, help lists no subcommands yet besides `--version`.
- [ ] **Step 8: Commit & push**, report. Message: `feat: project scaffold, strict config, cli skeleton`.

**Acceptance criteria:**
- `uv run repka --help` and `uv run repka --version` exit 0.
- Loading an empty config yields every boolean `False` (test).
- `worker_env()` never contains a token (test).
- `CLAUDE.md` exists and links the wiki.

---

### P1-T02: Spike: sqlite-vec loads under uv Python

**Req:** R-1.4, R-G2 · **Depends on:** T01 · **Size:** S · **Fallback:** numpy brute force ([ADR-002](../../../spec/decisions.md#adr-002-sqlite--sqlite-vec-with-numpy-fallback))
**Goal:** Know whether `vec0` indexes are usable on this Mac and how fast both paths are.

**Files:**
- Create: `scripts/spike_sqlite_vec.py`, `wiki/spikes/T02-sqlite-vec.md`, `tests/test_sqlite_vec_available.py`
- Modify: `pyproject.toml` (add `sqlite-vec==<exact latest version>`)

**Interfaces:**
- Produces: the spike verdict consumed by P1-T17: `GO` (use `vec0`) or `NO-GO` (numpy only); the exact pinned version.

- [ ] **Step 1:** `uv add 'sqlite-vec==<version>'` (pin exactly; record why that version).
- [ ] **Step 2: Write test** `test_sqlite_vec_knn_roundtrip`: open `sqlite3.connect(":memory:")`, `enable_load_extension(True)`, `sqlite_vec.load(conn)`, create `vec0(embedding float[4])`, insert 3 vectors, KNN for `[1,0,0,0]` returns the matching rowid first. If `enable_load_extension` is missing → `pytest.skip("extension loading unsupported: <python build>")`.
- [ ] **Step 3: Write `scripts/spike_sqlite_vec.py`**: prints Python version + `sys.executable`, `sqlite3.sqlite_version`, sqlite-vec version; inserts 5,000 random 768-dim float32 vectors; times 100 KNN queries (k=10) with `vec0` and with numpy cosine over a stacked matrix loaded from BLOBs.
- [ ] **Step 4: Run** `uv run pytest tests/test_sqlite_vec_available.py -v` and `uv run python scripts/spike_sqlite_vec.py`.
- [ ] **Step 5: Write `wiki/spikes/T02-sqlite-vec.md`**: environment, numbers, verdict `GO`/`NO-GO`, and the recommendation for T17. Add a "Status:" line under ADR-002 linking the spike page. Put the verdict in the ROADMAP Notes column.
- [ ] **Step 6: Commit & push**, report. Message: `chore: sqlite-vec spike and pinned dependency`.

**Acceptance criteria:** spike page has both timings and an explicit verdict; test passes or skips with a reason naming the Python build.

---

### P1-T03: Spike: local decider runs on this Mac

**Req:** R-1.3, R-G2 · **Depends on:** T01 · **Size:** M · **Fallback:** d1-2B (smaller) if d1-3B is too slow/heavy; Clef stays optional
**Goal:** Copy the proven decider kit from the reference project, run it on a labelled sample of repository texts, and measure speed and accuracy. Separately decide GO/NO-GO for a Clef MLX backend.

**Files:**
- Create (copy from the prototype's `classifier/`, then adapt paths/names): `deciders/models.yaml`, `deciders/setup.sh`, `deciders/fetch.py`, `deciders/README.md`, `deciders/workers/d1_worker.py`, `deciders/workers/pplx_worker.py`
- Create: `deciders/questions.yaml` (repo-triage questions from [deciders § 4](../../../research/deciders.md#4-triage-questions-for-repositories-default-set); **do not** copy the reference questions' contents), `tests/fixtures/triage_sample.jsonl`, `scripts/spike_decider.py`, `wiki/spikes/T03-deciders.md`

**Interfaces:**
- Produces: `deciders/` directory with the worker I/O contract unchanged ([deciders § 3](../../../research/deciders.md#3-integration-pattern-reused-from-the-reference-project)); `questions.yaml` with ids `relevant` (noul), `substance` (score, 5 levels), `kind` (choice), `is_junk` (noul, `enabled: false`); state templates with placeholders `{full_name} {description} {topics} {language} {stars} {is_fork} {readme_excerpt} {domain_profile}`; verdict on default model and Clef.

- [ ] **Step 1: Copy** the files listed; keep `models.yaml` revisions byte-identical; change only directory names/comments. `setup.sh` creates `deciders/.venv-liquid` and `.venv-pplx`.
- [ ] **Step 2: Write `deciders/questions.yaml`** with the four questions and two state templates.
- [ ] **Step 3: Build the sample** `tests/fixtures/triage_sample.jsonl`: 20 lines `{"full_name", "description", "topics", "language", "stars", "is_fork", "readme_excerpt", "label_relevant": bool, "label_substance": 0..4}` for the example domain "MCP servers & tooling" (10 clearly relevant tools, 10 junk/off-topic/empty). README excerpts ≤ 1,500 chars, taken from public repos via `gh api` (network is fine in a spike script, not in tests).
- [ ] **Step 4: Write `scripts/spike_decider.py`**: renders states from the sample + `questions.yaml`, writes `requests.json`, runs `deciders/.venv-liquid/bin/python -I deciders/workers/d1_worker.py --config '{"repo":..., "revision":...}' --requests ... --out ...` in a temp cwd, prints per-item answers, accuracy of `relevant p>=0.7` vs label, mean |substance − label|, load time and ms/item.
- [ ] **Step 5: Run** `./deciders/setup.sh && ./deciders/setup.sh --download d1-3B` (≈ 6 GB; if the agent lacks permission or disk, set status `blocked` and ask the human to run it), then `uv run python scripts/spike_decider.py`.
- [ ] **Step 6: Clef check** (time-boxed 1 hour): find the official Clef/Clef-flash model card; note RAM of this Mac (`sysctl hw.memsize`). If ≥ 16 GB and an MLX build loads via its `clef_mlx.py` in a throwaway venv, run the same sample. Record repo id, revision, RAM, speed, accuracy, or why NO-GO.
- [ ] **Step 7: Write `wiki/spikes/T03-deciders.md`** with tables for each model tried and verdicts: **default model** (d1-3B or d1-2B) and **Clef: GO/NO-GO**. Update the ROADMAP Notes for T03 and T16 (`dropped` if Clef NO-GO).
- [ ] **Step 8: Commit & push**, report. Message: `feat: decider kit copied from the prototype, triage questions, spike results`.

**Acceptance criteria:** d1 model answers all 20 sample items; spike page lists accuracy and ms/item; default model named; Clef verdict recorded with reasons. The `deciders/` workers compile (smoke test picks them up).

---

### P1-T04: Spike: Ollama embedder returns vectors

**Req:** R-1.4, R-G2 · **Depends on:** T01 · **Size:** S · **Fallback:** MLX embedding worker (adds a task to M4) if Ollama can't be used
**Goal:** Confirm the embedding endpoint, model name, dimension and speed.

**Files:**
- Create: `scripts/spike_ollama.py`, `wiki/spikes/T04-ollama.md`

**Interfaces:**
- Produces: confirmed values for `embed.model`, `embed.url`, vector `dim`, request/response JSON shape, and whether `search_document:` / `search_query:` prefixes are required; consumed by P1-T18.

- [ ] **Step 1:** Check `ollama --version`; if missing, status `blocked` with "install Ollama (brew install ollama) and run `ollama pull nomic-embed-text`".
- [ ] **Step 2: Write `scripts/spike_ollama.py`**: POST `{url}/api/embed` with `{"model": "nomic-embed-text", "input": [3 texts]}`; print dim, time for a batch of 32 README-sized texts, cosine similarity of two related vs two unrelated texts (sanity: related > unrelated).
- [ ] **Step 3: Run it**, write `wiki/spikes/T04-ollama.md` with JSON shapes, dim, timing, verdict. Update the default `embed.model` in architecture if different.
- [ ] **Step 4: Commit & push**, report. Message: `chore: ollama embedding spike`.

**Acceptance criteria:** dimension and response shape recorded; related-vs-unrelated sanity check passes.
