# M2: Store & Collector Engine

[← ROADMAP](../ROADMAP.md) · Prev: [M1](M1-domain-profile.md) · Next: [M3 Triage](M3-triage.md)

Goal of the milestone: `repka collect` fills SQLite with repositories found by each due domain's query matrix, plus metric snapshots, key-file versions, dependency stacks and events, and survives rate limits.

Contract: [architecture § 4 data model](../../spec/architecture.md#4-data-model-sqlite), [§ 5 pipeline](../../spec/architecture.md#5-pipeline-semantics) · Research: [github-api](../../research/github-api.md) · ADR: [ADR-007](../../spec/decisions.md#adr-007-github-data-access--rest-search--batched-graphql-enrichment)

Fixtures: recorded GitHub JSON lives in `tests/fixtures/github/`. Record them once with `gh api` (strip nothing secret — responses contain no token), keep them small (≤ 5 items per page) and name them after the scenario.

---

### P1-T09: SQLite store, migrations and repositories

**Req:** R-1.4 · **Depends on:** T01 · **Size:** M
**Goal:** A migrated SQLite DB and a small data-access layer every later task uses.

**Files:**
- Create: `src/repka/store/__init__.py`, `src/repka/store/db.py`, `src/repka/store/repo.py`, `src/repka/store/migrations/001_init.sql`, `src/repka/store/cli.py`
- Modify: `src/repka/cli.py` (register `db stats`, `db migrate`)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `config.db_path()`, `repka.models` (`RepoHit`, `Snapshot`, `KeyFile`, `Event`).
- Produces:
  - `repka.store.db`: `connect(path: Path | None = None, readonly: bool = False) -> sqlite3.Connection` (WAL, `foreign_keys=ON`, `busy_timeout=5000`, `row_factory=sqlite3.Row`; read-only via `file:...?mode=ro` URI); `migrate(conn) -> int` (applies `migrations/NNN_*.sql` above the stored version, each in a transaction; returns the new version); `utcnow() -> str` (ISO `...Z`, seconds).
  - `001_init.sql`: every table of architecture § 4 **except** `vec_index_*` (T17 creates those at runtime).
  - `repka.store.repo` (all take `conn` first; none commit — callers own transactions):
    `upsert_repo(conn, hit: RepoHit, now: str) -> bool` (True if new; updates `full_name` and attributes on existing id) ·
    `link_domain_repo(conn, domain_id: str, repo_id: int, query_key: str, now: str) -> None` ·
    `mark_repo_gone(conn, repo_id: int) -> None` ·
    `insert_snapshot(conn, s: Snapshot) -> None` · `latest_snapshot(conn, repo_id: int) -> Snapshot | None` · `snapshots_for(conn, repo_id: int, since: str | None = None) -> list[Snapshot]` ·
    `latest_key_file(conn, repo_id: int, path: str) -> tuple[str, list[str] | None] | None` · `insert_key_file(conn, kf: KeyFile, deps: list[str] | None, now: str) -> None` ·
    `domain_has_repo(conn, domain_id: str, repo_id: int) -> bool` (added by T11) · `add_event(conn, e: Event) -> int` ·
    `cursor_done(conn, domain_id: str, query_key: str, slice_key: str) -> bool` · `mark_cursor(conn, domain_id, query_key, slice_key, total_count: int, capped: bool, now: str) -> None` ·
    `@dataclass DomainState(domain_id: str, last_scan_at: str | None, last_scan_status: str | None)` · `get_domain_state(conn, domain_id) -> DomainState` (row-less → all None) · `set_domain_scanned(conn, domain_id, at: str, status: str, profile_hash: str, scan_every_s: int) -> None` ·
    `repos_for_domain(conn, domain_id: str, accepted_only: bool = False) -> list[sqlite3.Row]` (accepted = latest verdict row `accepted=1`) ·
    `start_run(conn, kind: str) -> int` · `finish_run(conn, run_id: int, status: str, stats: dict, error: str | None) -> None` ·
    `table_counts(conn) -> dict[str, int]` ·
    `class SqliteEtagCache(conn)` with `get(url: str) -> tuple[str, str] | None` and `put(url: str, etag: str, body: str) -> None` (matches `github.client.EtagCache`).

- [ ] **Step 1: Write failing tests**

```python
def test_migrate_creates_tables_and_is_idempotent(tmp_path): v1 = migrate(c); v2 = migrate(c); assert v1 == v2 == 1
def test_upsert_new_then_existing(conn): assert upsert_repo(conn, HIT, T0) is True; assert upsert_repo(conn, HIT, T1) is False
def test_upsert_rename_keeps_history(conn):  # RF3: same repo_id, new full_name -> one row, snapshots still attached
def test_mark_gone_keeps_snapshots(conn)
def test_cursor_roundtrip(conn)
def test_domain_state_defaults_none(conn)
def test_foreign_keys_enforced(conn)      # snapshot for unknown repo_id -> sqlite3.IntegrityError
def test_readonly_connection_rejects_writes(tmp_path)
def test_db_stats_cli(home, capsys)       # prints "repos 0" etc.
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS (+ full suite, ruff).
- [ ] **Step 5: Commit & push**, report. Message: `feat(store): sqlite schema, migrations, data access`.

**Acceptance criteria:** `uv run repka db migrate && uv run repka db stats` works on a fresh checkout and creates `.state/repka.db`.

---

### P1-T10: GitHub HTTP client

**Req:** R-1.2a, R-1.2b · **Depends on:** T01 · **Size:** M
**Goal:** One sync httpx client that handles auth, the search throttle, rate limits, retries, redirects and ETags — so nothing else in the code base thinks about them.

**Files:**
- Create: `src/repka/github/__init__.py`, `src/repka/github/client.py`
- Test: `tests/test_github_client.py`, `tests/fixtures/github/` (contributors_link.json + headers, participation.json, readme.md, search_page.json)

**Interfaces:**
- Consumes: `GithubConfig`, `ConfigError`.
- Produces (`repka.github.client`):
  - `class GitHubError(RuntimeError)`; `class RateLimited(GitHubError)` with attribute `retry_at: float | None`; `class AuthError(ConfigError)` (bad credentials).
  - `@dataclass(frozen=True) AuthInfo(login: str, expires_at: datetime | None)` (`expires_at` from the `github-authentication-token-expiration` response header, aware UTC; `None` = no expiry, e.g. the `gh` OAuth token).
  - `class EtagCache(Protocol)`: `get(url) -> tuple[str, str] | None`, `put(url, etag, body) -> None`.
  - `@dataclass SearchPage(total_count: int, incomplete: bool, items: list[dict])`.
  - `GitHubClient(token: str, cfg: GithubConfig, *, transport: httpx.BaseTransport | None = None, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, cache: EtagCache | None = None, token_source: Literal["env", "gh"] = "env")` with methods:
    `check_auth() -> AuthInfo` (`GET /user`; one request, not throttled as search) ·
    `search_repos(q: str, page: int = 1, per_page: int | None = None, sort: str = "stars", order: str = "desc") -> SearchPage` ·
    `search_total_count(q: str) -> int` ·
    `graphql(query: str, variables: dict) -> dict` (returns `data`; raises `GitHubError` only if `data` is missing; per-node `NOT_FOUND` errors are tolerated) ·
    `contributors_count(full_name: str) -> int | None` (`per_page=1&anon=true`; last page from `Link`; no `Link` → len(items); 403 "too large" → None) ·
    `participation(full_name: str) -> list[int] | None` (`all` series, 52 ints; 202 → None) ·
    `readme(full_name: str) -> str` (raw accept; 404 → "") ·
    `close() -> None`.
- Behaviour (fixed): base `https://api.github.com`; headers `Authorization: Bearer`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `User-Agent: repka-cli`; `follow_redirects=True`; search calls throttled to `cfg.search_per_minute` with a sliding 60 s window using `clock`/`sleep`; 401 on any call → `AuthError` whose message says the token is expired, revoked or invalid and gives the fix for its `token_source`: `env` → "create a new token at https://github.com/settings/tokens and replace REPKA_GITHUB_TOKEN in .env (or empty it to use the gh login)"; `gh` → "run `gh auth login`"; a 401 is never retried; 403/429 with `Retry-After ≤ 60` → sleep then retry once; larger `Retry-After`, a "secondary rate limit" body, or `x-ratelimit-remaining: 0` → `RateLimited`; 5xx/transport errors → 3 retries with 1, 2, 4 s; the token never appears in exception messages or logs.

- [ ] **Step 1: Write failing tests** (all with `httpx.MockTransport` and a fake clock/sleep):

```python
def test_headers_and_auth()
def test_search_throttle_sleeps_after_limit(): # search_per_minute=2, 3 calls at t=0 -> sleep called once with ~60
def test_secondary_limit_raises_rate_limited():  # RF1: 403 {"message":"You have exceeded a secondary rate limit"} + Retry-After: 120
def test_short_retry_after_sleeps_and_retries():  # 429 Retry-After: 5, then 200
def test_primary_limit_remaining_zero_raises()
def test_5xx_retries_then_github_error()
def test_401_is_auth_error_with_env_fix()         # token_source="env": message mentions REPKA_GITHUB_TOKEN and settings/tokens; isinstance ConfigError; not retried
def test_401_is_auth_error_with_gh_fix()          # token_source="gh": message mentions `gh auth login`
def test_check_auth_reads_login_and_expiry()      # header "2026-11-20 10:00:00 UTC" -> aware datetime
def test_check_auth_without_expiry_header()       # expires_at is None
def test_contributors_count_from_link_header(): # Link: <...&page=137>; rel="last" -> 137
def test_contributors_without_link_counts_items()
def test_participation_202_returns_none()
def test_readme_404_returns_empty()
def test_etag_304_returns_cached_body()
def test_token_not_in_error_messages()
def test_redirect_301_is_followed()
def test_graphql_tolerates_not_found_node_errors()
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Optional live check** (`REPKA_LIVE=1 uv run pytest -m live tests/test_github_client.py`): one real `search_total_count("topic:mcp")` > 0.
- [ ] **Step 6: Commit & push**, report. Message: `feat(github): rate-limit-aware client`.

**Acceptance criteria:** every error path in the list above has a test; no other module constructs `httpx.Client` for GitHub.

---

### P1-T11: Search with pagination and date bisection

**Req:** R-1.2a · **Depends on:** T06, T09, T10 · **Size:** M
**Goal:** Collect all repos for a domain's query matrix within the scan window, splitting date ranges until each slice is ≤ 1,000 results, resumable after interruption.

**Files:**
- Create: `src/repka/github/search.py`
- Test: `tests/test_search.py`, `tests/fixtures/github/search_item.json`

**Interfaces:**
- Consumes: `GitHubClient.search_repos`, `RateLimited`; `Query`, `Window`, `build_queries`, `scan_window`; store `upsert_repo`, `link_domain_repo`, `add_event`, `cursor_done`, `mark_cursor`.
- Produces (`repka.github.search`):
  - `@dataclass(frozen=True) Slice(field: str, start: date, end: date)` with `qualifier() -> str` (`created:2026-01-01..2026-03-31`), `key() -> str` (`created:2026-01-01..2026-03-31`), `days() -> int`, `halves() -> tuple[Slice, Slice]` (split at the midpoint date; the second half starts the day after the first ends).
  - `hit_from_item(item: dict) -> RepoHit`.
  - `@dataclass QueryStats(slices: int = 0, pages: int = 0, hits: int = 0, new: int = 0, capped: int = 0, incomplete: int = 0)`.
  - `collect_query(client, conn, domain_id: str, query: Query, window: Window, now: str, budget: int, emit_new: bool, *, per_page: int = 100) -> tuple[QueryStats, set[int]]` — returns stats and the repo ids seen; `budget` = max hits still allowed for this domain scan; `emit_new=False` on the domain's first scan (silent baseline).
  - `collect_search(client, conn, domain: Domain, window: Window, now: str, max_repos: int, emit_new: bool, *, per_page: int = 100) -> tuple[QueryStats, set[int]]` — over all queries, summed. **Callers (T13) pass `per_page=cfg.github.per_page`**: it is sent explicitly on every request and drives short-page detection and the page cap `ceil(1000 / per_page)` (10 at the default). `seen` only holds repos read in *this* call (slices skipped via `search_cursor` add nothing), so enrichment targets should come from `domain_repos`, not from `seen` alone. A finished bisected parent slice is also cursor-marked, so a resumed run does not re-probe it.

Algorithm (fixed): for the window as one slice, read page 1 (`per_page` from config, `sort=stars`). If `total_count > 1000` and `days() > 1` → recurse into `halves()` (discarding that page). If `total_count > 1000` at 1 day → keep it, mark `capped`, `log.warning("slice capped ...")`. Otherwise page until a page has fewer than `per_page` items or page 10. If `incomplete_results` on a page → retry that page once with `per_page=50` (adjusting page numbers) and count `incomplete`. Commit after every page. After a slice completes, `mark_cursor`. Skip slices where `cursor_done`. On the first sighting of a repo in this domain, add `Event(kind="new_repo", payload={"created_at": ...})` — only when `emit_new` is true. `RateLimited` propagates after committing finished pages.

- [ ] **Step 1: Write failing tests** with a `FakeSearchClient` that answers from a dict `{slice_qualifier: total_count}` and generates items deterministically:

```python
def test_hit_from_item_maps_fields()                 # fixture search_item.json -> RepoHit with repo_id == item["id"]
def test_pages_until_short_page()
def test_bisection_splits_when_over_1000()            # window 8 days, 2500 total -> slices of <=1000 only
def test_bisection_stops_at_one_day_and_caps()        # RF2: 1-day slice with 5000 -> one capped slice, 1000 hits, no deeper calls
def test_resume_skips_completed_slices()              # RF1: 2nd run after RateLimited mid-slice-3 re-reads only slices 3..n
def test_rate_limited_keeps_committed_pages()         # RF1
def test_new_repo_event_only_on_first_sighting()
def test_first_scan_is_silent_baseline()             # RF8: emit_new=False -> repos stored, zero new_repo events
def test_incomplete_results_retried_with_per_page_50()
def test_budget_caps_hits()
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit & push**, report. Message: `feat(github): search with date bisection and resume`.

**Acceptance criteria:** no slice ever returns > 1,000 items; recursion depth bounded by `log2(window days)`; tests above pass.

---

### P1-T12: Enrichment, snapshots, key files, stack, events

**Req:** R-1.2b, R-1.4 · **Depends on:** T09, T10 · **Size:** L
**Goal:** For a set of repos, record a metric snapshot, detect new releases and changed key files, parse dependency stacks, and emit events.

**Files:**
- Create: `src/repka/github/enrich.py`, `src/repka/github/stack.py`
- Test: `tests/test_enrich.py`, `tests/test_stack.py`, `tests/fixtures/github/graphql_nodes.json`, `graphql_null_node.json`, `graphql_blob_texts.json`, `tests/fixtures/stack/` (pyproject_pep621.toml, pyproject_poetry.toml, package.json, Cargo.toml, go.mod, broken.toml)

**Interfaces:**
- Consumes: `GitHubClient.graphql/contributors_count/participation`; store `latest_snapshot`, `insert_snapshot`, `latest_key_file`, `insert_key_file`, `add_event`, `mark_repo_gone`; `CollectConfig.enrich_batch`, `slow_metrics_every`.
- Produces:
  - `repka.github.stack.parse_deps(path: str, text: str) -> list[str]` — sorted, unique, lowercase; Python names PEP 503-normalized (`re.sub(r"[-_.]+", "-", name)`), version specifiers/extras/markers stripped; pyproject: `[project].dependencies` + `[tool.poetry].dependencies` keys minus `python`; package.json: `dependencies` + `devDependencies` keys; Cargo.toml: `[dependencies]` keys; go.mod: module paths in `require`. Unparseable → `[]` with a warning, never raises.
  - `repka.github.enrich.build_enrich_query(key_files: Sequence[str]) -> tuple[str, dict[str, str]]` — GraphQL text using `$ids: [ID!]!`, `$since7`, `$since30`, `$since90` (GitTimestamp), aliases `kf0..kfN` → path map.
  - `snapshot_from_node(node: dict, now: datetime, prev: Snapshot | None) -> Snapshot` — empty repo (`defaultBranchRef: null`) → commits 0; `releases_90d` = releases created ≥ now − 90 d; contributors / active_weeks_12 carried over from `prev` (refreshed separately).
  - `@dataclass EnrichStats(repos: int = 0, gone: int = 0, events: int = 0, slow_refreshed: int = 0)`.
  - `enrich_repos(client, conn, domain_id: str, repo_ids: Sequence[int], key_files: Sequence[str], now: datetime, cfg: CollectConfig, slow_for: set[int]) -> EnrichStats` — `slow_for` = accepted repo ids (only they get REST slow metrics).

Rules (fixed):
- Batches of `cfg.enrich_batch` node ids; commit after each batch.
- `node is None` → `mark_repo_gone`.
- **Release event** when the new `latest_release_tag` differs from `prev.latest_release_tag` and `prev` exists; on first sighting only if `latest_release_at >= now − 7 days`. Payload `{tag, name, published_at, url}`.
- **Key files:** if the blob oid differs from `latest_key_file`, fetch texts for the changed paths in one extra GraphQL query (`... on Blob { text }`), `parse_deps`, `insert_key_file`. If a previous version existed: `key_file_changed {path, old_oid, new_oid}`; if deps differ: `stack_changed {path, added: [...], removed: [...]}`. First sighting → no events. File missing → nothing stored.
- **Slow metrics** (`contributors_count`, `participation` → `active_weeks_12` = non-zero weeks among the last 12) **only for repos in `slow_for`** (accepted), when the last non-null `contributors` snapshot is older than `slow_metrics_every` or absent; `None` results are stored as `NULL` and retried next run. This keeps a large first scan within the 5,000/h core budget (RF9).

- [ ] **Step 1: Write failing tests**

```python
# test_stack.py
def test_parse_pep621(): assert parse_deps("pyproject.toml", PEP621) == ["httpx", "pydantic", "typing-extensions"]
def test_parse_poetry_skips_python()
def test_parse_package_json_includes_dev()
def test_parse_cargo(); def test_parse_go_mod()
def test_parse_broken_returns_empty(caplog)
# test_enrich.py (FakeGraphQLClient returning fixtures)
def test_build_query_has_alias_per_key_file()
def test_snapshot_from_node()                         # stars/forks/commits_7d/commits_30d/releases_90d from graphql_nodes.json
def test_empty_repo_commits_zero()
def test_null_node_marks_repo_gone()                  # RF3
def test_release_event_on_new_tag()
def test_no_release_event_for_old_release_on_first_sighting()
def test_key_file_change_emits_change_and_stack_events()
def test_first_key_file_sighting_emits_nothing()
def test_slow_metrics_only_when_due()
def test_slow_metrics_only_for_accepted()            # RF9: repo not in slow_for -> no contributors/participation calls
def test_participation_202_stores_null_and_retries_next_time()
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `stack.py`, then `enrich.py`. **Step 4: Run** → PASS.
- [ ] **Step 5: Optional live check:** enrich 3 well-known repos with `REPKA_LIVE=1`; verify numbers match the GitHub UI roughly.
- [ ] **Step 6: Commit & push**, report. Message: `feat(github): enrichment snapshots, key files, stack and events`.

**As built (deviations, P1-T12):** (1) the slow-metric refresh rule uses new stamps `repos.contributors_at` / `participation_at` (migration `002_slow_metrics_at.sql`, helpers `slow_metrics_fetched_at` / `mark_slow_metrics` in `store/repo.py`), tracked per metric, because snapshots carry the last values forward and "last non-null snapshot" would never go stale; (2) `build_enrich_query` declares only `$ids`, `$since7`, `$since30` (an unused `$since90` is a GraphQL validation error) and `releases_90d` is counted from the last 10 releases; (3) a repo whose latest snapshot already has the same `taken_at` second is skipped (same repo in two domains within one run); (4) the changed-blob text query uses the Blob node `id` (`nodes(ids:) { ... on Blob { text } }`).

**Acceptance criteria:** all metrics from R-1.2b land in `snapshots`/`key_files`; events emitted only by the rules above; one GraphQL call per 50 repos (+1 per batch with changed key files).

---

### P1-T13: `repka collect` command

**Req:** R-1.2a, R-1.2b · **Depends on:** T11, T12 · **Size:** S
**Goal:** Collect + enrich all due domains (or one), record domain scan state, handle rate limits across domains.

**Files:**
- Create: `src/repka/collect.py`, `src/repka/collect_cli.py`
- Modify: `src/repka/cli.py`
- Test: `tests/test_collect.py`

**Interfaces:**
- Consumes: `load_domains`, `get_domain`, `profile_hash`, `build_queries`, `scan_window`, `collect_search`, `enrich_repos`, store `get_domain_state`, `set_domain_scanned`, `repos_for_domain`.
- Produces (`repka.collect`):
  - `due_domains(domains: list[Domain], states: dict[str, DomainState], now: datetime) -> list[Domain]` — enabled and (never scanned or `now − last_scan_at >= scan_every`).
  - `@dataclass DomainReport(domain_id: str, status: Literal["ok","partial","skipped"], search: QueryStats, enrich: EnrichStats, error: str | None)`; `@dataclass CollectReport(domains: list[DomainReport], rate_limited: bool)`.
  - `collect_domains(client, conn, domains: list[Domain], cfg: AppConfig, now: datetime) -> CollectReport` — per domain: search (`emit_new = last_scan_at is not None`) → enrich (seen ids ∪ accepted ids of the domain; `slow_for` = accepted ids) → `set_domain_scanned(status="ok")`. On `RateLimited`: domain status `partial` (its `last_scan_at` **not** advanced), remaining domains `skipped`, `rate_limited=True`, return normally.
  - CLI `repka collect [--domain ID] [--dry-run]`: `--dry-run` prints each due domain's window and query count and makes no network calls; `--domain` collects that domain even if not due (but refuses a disabled domain with a `ConfigError` hint).

- [ ] **Step 1: Write failing tests**

```python
def test_due_domains_logic()                         # never scanned -> due; scanned 1h ago with 12h -> not due; disabled -> never
def test_rate_limit_stops_remaining_domains_and_marks_partial()
def test_last_scan_not_advanced_on_partial()
def test_dry_run_makes_no_client(monkeypatch)        # client factory raises if called
def test_collect_cli_without_token_exits_2(home)
def test_disabled_domain_with_flag_is_config_error(home)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Manual live run** (human or agent with a token): enable a copy of the example domain with a narrow `first_window: 30d`, run `uv run repka collect --domain <id>`, then `uv run repka db stats`. Record counts and duration in the report.
- [ ] **Step 6: Commit & push**, report. Message: `feat: repka collect`.

**As built (deviations, P1-T13):** (1) `last_scan_at` advances only on `ok`; `partial` is recorded by `set_domain_scanned` with the *old* `last_scan_at` (possibly NULL), so `emit_new = last_scan_at is not None` means "a complete baseline exists" and an interrupted first scan stays silent. (2) `scan_window` gets the stored `last_scan_at`. (3) `search_cursor` rows are deleted (`store.repo.clear_cursors`) when a domain scan completes, so cursors exist only for an unfinished scan and a same-day forced rerun really searches. (4) `due_domains` also returns a domain whose last scan was `partial` (resume now, not after `scan_every`). (5) Enrichment targets come from `domain_repos`, not from `seen`: matched after their latest snapshot ∪ never snapshotted ∪ accepted, minus `gone` (so a resumed scan does not re-snapshot what the interrupted run already did); `slow_for` = accepted ids. (6) A scan that hits `max_repos_per_domain_scan` is `partial` (resumable through the cursor). (7) `collect_search` got an optional `progress(index, count, query, stats)` callback; `collect_domains` an optional `progress: Callable[[str], None]` keyword; `CollectReport.retry_at` (wall-clock epoch) and `DomainReport` field defaults added. (8) A non-rate-limit `GitHubError` marks the domain `partial` and propagates (CLI: `error: ...`, exit 1); `AuthError` exits 2. (9) CLI: `--json`; `--dry-run` is read-only (no migration, lock or client); a real run migrates, takes `.state/run.lock` (`LockBusy` → stderr, exit 0), writes a `runs` row (`kind=collect`); nothing due → "nothing due" plus each domain's next due time, and no token needed; rate-limited → `partial`, exit 0.

**Acceptance criteria:** a real collect of a small domain completes within GitHub limits and a second immediate run reports the domain as not due.
