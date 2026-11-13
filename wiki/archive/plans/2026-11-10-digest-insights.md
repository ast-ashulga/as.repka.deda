# Digest Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work test-first inside each task (superpowers:test-driven-development).

**Goal:** Make the Telegram digest show only what is new and rising (with decider facet tags per repo) and move releases into their own once-a-day message, all behind flags that default to `false`.

**Architecture:** Extend the existing digest pipeline (`src/repka/digest/`): `build.py` gets an `insights` selection mode and facet extraction from `verdicts.answers_json`; `render.py` gets a multi-line item format; a new `digest/releases.py` builds, renders and records a separate releases message (its own `digests` row, `kind='releases'`); two read-only CLI additions (`repka digest --more`, `repka releases`). `pipeline.run_pass` runs the releases message as one more stage that never blocks the others.

**Tech Stack:** Python 3.12, SQLite (migrations in `src/repka/store/migrations/`), argparse CLI, pytest, ruff, `uv`.

**Spec:** `wiki/spec/2026-11-10-digest-insights-design.md` (read it first; this plan only adds decisions it leaves open).

## Global Constraints

- Every new boolean flag defaults to `false`: `digest.insights`, `digest.releases_message`. With both `false` the digest output is byte-identical to today (pin this with a test).
- New config keys (exact names/defaults): `digest.insights: false`, `digest.releases_message: false`, `digest.releases_slot: "09:00"`, `digest.facet_min_p: 0.7`, `digest.pending_max_age: 7d`. Strict loader: unknown keys/wrong types raise `ConfigError` naming the dotted key.
- Facet tags: at most 3 per repo (`MAX_FACETS = 3`), only `noul` answers whose id is not in `{"relevant", "substance", "kind", "is_junk"}` and whose `p >= digest.facet_min_p`; label = question id with `_` replaced by a space.
- Untrusted text (repo names, descriptions, tags, release tags): `textutil.printable()` before terminal output, `textutil.html_escape()` before Telegram (render through the existing `_text` helper in `render.py`).
- Tests never touch the network, GitHub, Telegram, Ollama or a model; use `tests/helpers_digest.py`, fakes and `httpx.MockTransport`.
- Empty messages are never sent (also for the releases message). No real-time alerts.
- Repos are keyed by numeric `repo_id`. No concurrency against GitHub (untouched here).
- `migrate()` must stay race-safe: new migration file `004_*.sql` inserts its own `schema_version` row (see `003_landscape_cache.sql`).
- Definition of Done per task (AGENT-GUIDE §3): `uv run pytest -q` and `uv run ruff check src tests` pass; new keys/commands are mirrored in `wiki/spec/architecture.md`.
- Commits: project rule lets agents commit and push (never force-push). Do not commit `domains/*.yaml` question edits unless the owner says so; they are already uncommitted in the working tree.
- Model per task (AGENT-GUIDE §2a): Haiku for Tasks 1, 7, 9-docs; Sonnet advised by Opus for Tasks 3, 6, 8.

## Review Focus

Failure modes the spec implies that a reader would hit; each has a test in the named task.

1. A repo whose latest verdict has no facet answers (decided under the old question set) must render without tags and without error (Task 2).
2. A release event of a *rejected* repo while `releases_message` is on must be consumed by the releases message, never shown in the main digest (Task 6).
3. A failed releases send leaves its events pending; the next run sends them once; a third run sends nothing (Task 6).
4. Descriptions or tags containing `<`, `&` or control characters must stay escaped in the new multi-line item format, and one item is never split across Telegram messages (Task 4).
5. `releases_slot` equal to a main slot (e.g. `"08:30"`) must not collide with the main digest's `digests` row, and running the stage twice sends once (Task 6).

---

### Task 1: Config keys and doctor warning

**Files:**
- Modify: `src/repka/config.py` (`DigestConfig` ~line 105, `load_digest` ~line 271)
- Modify: `src/repka/preflight.py` (follow the pattern of the triage check at ~line 148)
- Modify: `.config.yaml` (tracked defaults file; add the five keys with their defaults, flags `false`)
- Test: `tests/test_config.py`, `tests/test_preflight.py`

**Interfaces:**
- Produces: `DigestConfig.insights: bool = False`, `.releases_message: bool = False`, `.releases_slot: str = "09:00"`, `.facet_min_p: float = 0.7`, `.pending_max_age: timedelta = timedelta(days=7)`; preflight check id `digest-flags` (warning, not error).

- [ ] **Step 1: Write failing tests**
  - `test_digest_new_keys_default_off`: `AppConfig().digest` has `insights is False`, `releases_message is False`, `releases_slot == "09:00"`, `facet_min_p == 0.7`, `pending_max_age == timedelta(days=7)`.
  - `test_digest_new_keys_parse`: `load_digest({"insights": True, "releases_slot": "07:15", "facet_min_p": 0.5, "pending_max_age": "3d"})` returns those values.
  - `test_digest_bad_values_name_the_key`: `{"releases_slot": "9am"}`, `{"facet_min_p": 1.5}`, `{"insights": "yes"}` each raise `ConfigError` whose message contains `digest.releases_slot` / `digest.facet_min_p` / `digest.insights`.
  - `test_preflight_warns_when_only_one_digest_flag_is_on` in `tests/test_preflight.py`: flags (`True`, `False`) and (`False`, `True`) give a `digest-flags` warning; (`False`, `False`) and (`True`, `True`) give none.
- [ ] **Step 2: Run** `uv run pytest tests/test_config.py tests/test_preflight.py -q` → new tests FAIL.
- [ ] **Step 3: Implement** the fields and loader entries (`_bool`, a new `_slot` parser that validates `HH:MM` with `datetime.strptime` inline, since `repka.digest.slots` imports `config` and would be circular; `_number` plus a range check `0 < p <= 1`; `_duration`). Add `check_digest_flags(cfg: AppConfig)` in `preflight.py` returning the warning list, and register it where the other checks are collected. Update `.config.yaml`.
- [ ] **Step 4: Run** the same command → PASS; `uv run repka doctor` still `all good` on the real home.
- [ ] **Step 5: Commit** `feat(config): digest insights and releases-message keys, all off by default`.

---

### Task 2: Facet extraction and item enrichment

**Files:**
- Modify: `src/repka/digest/build.py`
- Modify: `tests/helpers_digest.py` (`add_verdict` gains `extra_answers: dict | None = None`, merged into the answers JSON)
- Test: `tests/test_digest_build.py`

**Interfaces:**
- Produces: `build.BASE_QUESTIONS: frozenset[str]`, `build.MAX_FACETS = 3`, `facet_tags(answers: dict[str, Any], min_p: float) -> list[tuple[str, float]]` (sorted by `p` desc then id). When `cfg.insights` is true, `DigestItem.text_fields["facets"]` is that list for 🆕/🔎/📈 items; 📈 items also carry `description`, `kind`, `level`, `stars`. When `insights` is false no new field is added.

- [ ] **Step 1: Write failing tests**
  - `test_facet_tags_filters_sorts_and_caps`: answers with `relevant 0.99`, `substance` score, `kind` choice, `a 0.95`, `b 0.80`, `c 0.75`, `d 0.72`, `e 0.60`, and `is_junk 0.99` → `facet_tags(..., 0.7) == [("a", 0.95), ("b", 0.8), ("c", 0.75)]`.
  - `test_facet_tags_empty_without_noul_extras`: answers with only base questions → `[]`; `answers == {}` → `[]`.
  - `test_insights_items_carry_facets` (Review Focus 1): verdict with `extra_answers={"self_modifies": {"type": "noul", "p": 0.94, "pred": True, "conf": 0.94}}` → the 🆕 item's `text_fields["facets"] == [("self_modifies", 0.94)]`; a second repo whose verdict has no extras → `[]`.
  - `test_insights_off_adds_no_facets_field`: same data with `cfg.insights False` → `"facets" not in item.text_fields`.
  - `test_mover_item_enriched_with_description_and_kind` under insights.
- [ ] **Step 2: Run** `uv run pytest tests/test_digest_build.py -q` → FAIL.
- [ ] **Step 3: Implement** `facet_tags` and thread `cfg.insights` / `cfg.facet_min_p` into the `new_cands` and `mover_cands` loops of `_domain_section` (movers: select `r.description` too and reuse `_num`-style answer reads for `kind`/`level`).
- [ ] **Step 4: Run** → PASS, then full `uv run pytest -q`.
- [ ] **Step 5: Commit** `feat(digest): facet tags from decider answers`.

---

### Task 3: Insights selection semantics

**Files:**
- Modify: `src/repka/digest/build.py` (`DigestSection`, `Digest`, `_domain_section`, `build_digest`)
- Test: `tests/test_digest_build.py`

**Interfaces:**
- Consumes: Task 1 config, Task 2 `facet_tags`.
- Produces: `DigestSection.more_by_kind: dict[str, int]` (keys are `line_kind`: `new`, `found`, `mover`, `stack`, `release`; `more` stays the total); `Digest.insights: bool = False` (last field, defaulted); behaviour below.

Behaviour (spec section "Semantics refined after review"):
- `cfg.releases_message` true → `release` events are skipped entirely in the main digest (not shown, not consumed, rejected repos included).
- `cfg.insights` true → fill order is new, movers, stack, releases (releases last, sharing the per-domain limit); false → today's order (new, releases, movers, stack).
- `cfg.insights` true → overflow `new_repo` events stay pending (not added to `consumed`); `new_repo` events with `detected_at` older than `now - cfg.pending_max_age` are consumed without being shown. Overflow `stack`/`release` events are still consumed as today. `cfg.insights` false → overflow consumed immediately, as today.

- [ ] **Step 1: Write failing tests**
  - `test_releases_message_on_leaves_release_events_alone`: accepted and rejected repos with `release` events, `cfg=DigestConfig(releases_message=True)` → no `releases` items, and those event ids appear in neither `shown_event_ids` nor `consumed_event_ids`.
  - `test_insights_overflow_stays_pending`: 3 new repos, `max_items_per_domain=2`, `insights=True` → 2 shown, `section.more == 1`, `more_by_kind == {"new": 1}`, the overflow event id not in `consumed_event_ids`; with `insights=False` the same id IS consumed.
  - `test_insights_expires_old_pending_new_repos`: event `detected_at` 8 days before `now`, `pending_max_age=7d` → not shown, id in `consumed_event_ids`.
  - `test_insights_order_puts_releases_last`: one of each kind, `max_items_per_domain=3`, `insights=True` → shown kinds are new, mover, stack; the release is counted in `more_by_kind["release"]`; with `insights=False` the order is new, release, mover (unchanged).
  - `test_flags_off_is_identical_to_before`: render the existing fixture digest with default `DigestConfig()` and assert it equals the pre-change snapshot string stored in the test (capture it before changing code).
- [ ] **Step 2: Run** `uv run pytest tests/test_digest_build.py -q` → FAIL.
- [ ] **Step 3: Implement** in `_domain_section`: add `e.detected_at` to the events query; branch the candidate/target loop on `cfg.insights`; fill `more_by_kind`; `build_digest` sets `Digest.insights = cfg.insights`. Keep the `False`/`False` path code-identical.
- [ ] **Step 4: Run** → PASS, then full suite.
- [ ] **Step 5: Commit** `feat(digest): insights selection (releases routing, pending overflow, expiry)`.

---

### Task 4: Insights rendering

**Files:**
- Modify: `src/repka/digest/render.py` (`_repo_line`, `_mover_line`, `render_digest`)
- Test: `tests/test_digest_render.py`

**Interfaces:**
- Consumes: `Digest.insights`, `DigestSection.more_by_kind`, `text_fields["facets"]`.
- Produces: with `d.insights` true each 🆕/🔎/📈 item is ONE list element of three lines joined by `\n`: `ICON <a>name</a> ★stars` (📈 adds ` <i>(+N★/24h)</i>`), then `<i>description</i>` truncated to `DESC_CHARS`, then `<code>kind · lvl N</code>` followed by ` · label` per facet. After the sections: a legend line listing only the icons present (`🆕 new repo · 🔎 newly found · 📈 star spike · 🧩 stack change · 🚀 release`) plus `tags = decider answers (p ≥ 0.70)`, and, if any `more`, `…and N more: 5 🆕, 7 🔎 (repka digest --more)` (sum over sections). With `insights` false the output is unchanged.

- [ ] **Step 1: Write failing tests**
  - `test_insights_item_is_three_lines_with_tags`: item with facets `[("self_modifies", 0.94)]` renders `<code>framework · lvl 3</code> · self modifies` on its third line.
  - `test_insights_item_stays_in_one_message` (Review Focus 4): `split_messages(render_digest(d))` with enough items to need two messages never puts a `\n`-joined item across a boundary.
  - `test_insights_escapes_untrusted_text` (Review Focus 4): description `<b>&\x07x`, facet id `a<b`, → no raw `<b>` from the description, no `\x07`, `&amp;` present.
  - `test_insights_legend_lists_only_present_icons` and `test_insights_more_counter_by_kind`: counter text equals `…and 12 more: 5 🆕, 7 🔎 (repka digest --more)` for `more_by_kind` `{"new": 5, "found": 7}`.
  - `test_render_unchanged_when_insights_off` (reuse Task 3's snapshot).
- [ ] **Step 2: Run** `uv run pytest tests/test_digest_render.py -q` → FAIL.
- [ ] **Step 3: Implement** `render_digest` branch on `d.insights`; new `_insight_item(item) -> str`; `_text` for every untrusted value. Keep `_release_line` for insights+releases-in-main.
- [ ] **Step 4: Run** → PASS, full suite.
- [ ] **Step 5: Commit** `feat(digest): insights message format`.

---

### Task 5: `repka digest --more`

**Files:**
- Modify: `src/repka/digest/build.py`, `src/repka/digest/cli.py`
- Test: `tests/test_digest_build.py`, `tests/test_digest_run.py` (CLI cases live there)

**Interfaces:**
- Produces: `pending_overflow(conn, domains, now, cfg: DigestConfig, slot: str, local_date: str) -> Digest`: a digest of exactly the items that a normal build would hold back (build once with `dataclasses.replace(cfg, max_items_per_domain=10**9)`, once with `cfg`, subtract by `(domain_id, repo_id, line_kind)`). CLI: `repka digest --more` (in the existing mutually exclusive mode group with `--send`/`--dry-run`), read-only connection, prints rendered parts; prints `nothing held back` when empty.

- [ ] **Step 1: Write failing tests**
  - `test_pending_overflow_returns_only_held_back_items`: 3 new repos, limit 2 → overflow digest has exactly the lowest-ranked repo.
  - `test_pending_overflow_writes_nothing`: events' `digest_id` all still `NULL` and `digests` has no new row.
  - `test_cli_more_prints_held_back_and_exits_0` and `test_cli_more_conflicts_with_send` (argparse exit 2).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(digest): digest --more lists held-back items`.

---

### Task 6: Releases message (build, render, record, run)

**Files:**
- Create: `src/repka/store/migrations/004_digest_kind.sql`
- Create: `src/repka/digest/releases.py`
- Modify: `src/repka/digest/run.py` (`_insert_digest`, `_record_skipped` take `kind`), `src/repka/digest/slots.py` (`recorded_slots(conn, since_date, kind="main")`, `slot_key`)
- Modify: `src/repka/dashboard_data.py:179` only if its digest list breaks (it selects `slot`; a `releases:09:00` value is acceptable, no change expected)
- Test: `tests/test_digest_releases.py` (new), `tests/test_store.py` (migration)

**Interfaces:**
- Produces: migration 004: `ALTER TABLE digests ADD COLUMN kind TEXT NOT NULL DEFAULT 'main';` then `INSERT INTO schema_version (version) VALUES (4);`. `slots.slot_key(kind: str, slot: str) -> str` (`slot` itself for `main`, else `f"{kind}:{slot}"`); the `digests` row for a releases message stores `slot = slot_key("releases", "09:00")` and `kind = 'releases'`, so the existing `UNIQUE (slot, local_date)` keeps it apart from the main row (deviation from the spec's `(kind, slot, local_date)` key: avoids rebuilding a table that `events` and `digest_items` reference; record it in the spec).
- Produces in `releases.py`:
  - `@dataclass ReleaseSection(domain_id: str, domain_name: str, items: list[DigestItem], more: int)`
  - `@dataclass ReleasesDigest(slot: str, local_date: str, sections: list[ReleaseSection], shown_event_ids: list[int], consumed_event_ids: list[int])` with `item_count` property (shown items)
  - `build_releases(conn, domains, now, cfg: DigestConfig, slot: str, local_date: str) -> ReleasesDigest`
  - `render_releases(d: ReleasesDigest) -> list[str]` (empty list when nothing to show)
  - `run_releases(conn, domains, cfg: AppConfig, sender: Sender | None, now: datetime, send: bool, slot: str | None = None, tz: ZoneInfo | None = None) -> DigestOutcome`
- Behaviour: accepted repos only, latest release event per repo (older events of the same repo are consumed with it), sorted by stars desc then `repo_id`, capped at `cfg.digest.max_items_per_domain` per domain, overflow consumed and counted in `more`; rejected repos' release events consumed; untriaged repos' events left pending. Header `<b>repka</b> · releases · YYYY-MM-DD`; section `<b>Name</b> · N releases` (N = shown + more); lines `🚀 <a>name</a> ★{k-format} · <code>tag</code>`; `…and N more`; footer `Full list: repka releases --since 24h`. No pre-release label (the `release` event payload has no such field). Slot decision: `decide_slot([cfg.digest.releases_slot], local, recorded, cfg.digest.max_lateness)` with `recorded_slots(..., kind="releases")`. Send/record mirrors `run_digest`: `send=False` is a pure preview; on `result.sent < 1` return `failed` and write nothing; on success one transaction writes the `sent` row, stamps `events.digest_id` for shown + consumed ids, inserts `digest_items` rows with kind `release`. Empty → `empty` row, nothing sent.

- [ ] **Step 1: Write failing tests** (all in `tests/test_digest_releases.py` unless noted)
  - `test_migration_004_adds_kind_default_main` (`tests/test_store.py`): after `migrate`, `digests` has `kind`, old rows read `'main'`, `schema_version` max is 4; running `migrate` twice is a no-op.
  - `test_latest_release_per_repo_sorted_by_stars`: two events for one repo → one line with the newer tag; repos ordered by stars desc.
  - `test_rejected_repo_release_consumed_not_shown` (Review Focus 2): rejected repo's event id is in `consumed_event_ids`, not `shown_event_ids`; untriaged repo's event in neither.
  - `test_limit_and_more_counter`: 12 repos, limit 10 → 10 lines, `…and 2 more`, the 2 overflow ids consumed.
  - `test_render_releases_exact_lines`: `🚀 <a href="https://github.com/o/r1">o/r1</a> ★81k · <code>v3.74.1</code>`, header, `· 2 releases`, footer text.
  - `test_failed_send_keeps_events_pending_then_sent_once` (Review Focus 3): sender returns `SendResult(ok=False, sent=0, error="x")` → `failed`, all `digest_id` still `NULL`, no `digests` row; second run with a good sender → `sent`; third run → `not_due`, sender not called.
  - `test_releases_slot_equal_to_main_slot_does_not_collide` (Review Focus 5): `releases_slot="19:30"` with a main row already recorded for `19:30` → the releases message still sends, two distinct `digests` rows exist.
  - `test_empty_releases_not_sent_and_recorded_empty`; `test_preview_writes_nothing`; `test_missed_slot_older_than_max_lateness_is_skipped_events_stay_pending`.
  - `test_main_recorded_slots_ignore_releases_rows` (`tests/test_digest_slots.py`): `recorded_slots(conn, d)` (default `main`) does not include the releases row; `recorded_slots(conn, d, kind="releases")` returns `("09:00", date)` with the prefix stripped.
- [ ] **Step 2: Run** `uv run pytest tests/test_digest_releases.py tests/test_store.py tests/test_digest_slots.py -q` → FAIL.
- [ ] **Step 3: Implement** migration, slot helpers, `kind` parameter in `run.py` helpers (default `"main"`, existing call sites unchanged), then `releases.py`. Reuse `_latest_verdicts`, `_stars` from `build.py`, `_text`/`_link`/`_stars` formatting from `render.py`, `split_messages`.
- [ ] **Step 4: Run** the three files, then full suite and `uv run ruff check src tests` → PASS.
- [ ] **Step 5: Commit** `feat(digest): separate daily releases message`.

---

### Task 7: `repka releases` command

**Files:**
- Create: `src/repka/releases_cli.py`
- Modify: `src/repka/cli.py` (register next to `digest_cli.register(sub)`)
- Test: `tests/test_releases_cli.py`

**Interfaces:**
- Produces: `repka releases [--since DURATION] [--domain ID]`, default `--since 24h` (`config.parse_duration`), read-only (`connect(readonly=True)`), reads `events` where `kind='release'` and `detected_at >= now - since`, regardless of `digest_id`; accepted repos only; latest event per repo; one section per enabled domain (or the one named), lines `★stars  full_name  tag  published_at`, sorted by stars desc; every string through `printable()`; `no releases in the last 24h` when empty; exit 0. A missing DB raises `ConfigError` (exit 2) like `repka digest`.

- [ ] **Step 1: Write failing tests**: `test_lists_sent_and_pending_events` (events with and without `digest_id` both appear), `test_since_window_excludes_old`, `test_rejected_repo_hidden`, `test_domain_filter`, `test_untrusted_name_is_printable`, `test_every_command_registered` (command exists in `build_parser()`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(cli): repka releases lists recent release events`.

---

### Task 8: Pipeline wiring

**Files:**
- Modify: `src/repka/pipeline.py` (`Boundaries`, `PassReport`, `run_pass`, `build_boundaries`' `digest_stage` neighbour, `report_dict`, hints), `src/repka/run_cli.py` (`_digest_line` neighbour)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `releases.run_releases`.
- Produces: `Boundaries.releases: Callable[[datetime, bool], DigestOutcome] | None = None` (appended after `close`, so existing constructions keep working); `PassReport.releases: DigestOutcome | None = None`; `report_dict(...)["releases"]` (same shape as `digest`, `parts` dropped when `full=False`); run output line `releases ...` mirroring the digest line. The stage runs only when `cfg.digest.releases_message` is true, after the digest stage; a `failed` outcome or exception counts as a problem exactly like the digest stage and never blocks other stages.

- [ ] **Step 1: Write failing tests**: `test_releases_stage_skipped_when_flag_off` (boundary not called, `report.releases is None`), `test_releases_stage_runs_after_digest_when_on`, `test_releases_failure_makes_pass_partial_not_failed`, `test_report_dict_has_releases_key`.
- [ ] **Step 2: Run** `uv run pytest tests/test_pipeline.py -q` → FAIL. **Step 3: Implement**, including the `releases_stage(now, send)` closure that mirrors `digest_stage`. **Step 4: Run** → PASS, full suite.
- [ ] **Step 5: Commit** `feat(pipeline): releases message stage`.

---

### Task 9: End-to-end test and docs

**Files:**
- Modify: `tests/test_e2e.py`
- Modify: `wiki/spec/architecture.md`, `README.md`, `.claude/skills/repka-guide/references/config-reference.md`, `wiki/spec/2026-11-10-digest-insights-design.md`

- [ ] **Step 1: Write the e2e test** `test_pass_with_insights_and_releases_message`: a full `run_pass` on the existing fixtures with `DigestConfig(insights=True, releases_message=True)` and a fake sender → exactly two messages sent (main digest with legend and facet tags, no `🚀`; releases message with `🚀` lines); a second pass sends nothing. Run → FAIL before docs/wiring gaps are found, PASS after fixes.
- [ ] **Step 2: Docs.** Architecture: the five config keys, `digests.kind`/slot-prefix rule, the two commands, the flag-combination table. README: user-facing section for both messages and both commands. `config-reference.md`: five new rows. Spec: record the slot-prefix deviation (instead of a `(kind, slot, local_date)` key), the dropped pre-release label (payload has no such field), and that overflow `stack`/`release` items are still consumed.
- [ ] **Step 3: Verify** `uv run pytest -q`, `uv run ruff check src tests`, `uv run repka --help` lists `releases`, `uv run repka digest --more` works on the real home (read-only), `uv run repka doctor` is `all good`.
- [ ] **Step 4: Commit** `docs: digest insights and releases message`.
- [ ] **Step 5: Rehearsal (owner's call, not automatic).** Show the owner `REPKA_HOME=<scratch copy> uv run repka run` with both flags set to `true` in the scratch `.config.yaml` (preview, nothing sent). Enabling the flags in the real `.config.yaml` needs the owner's explicit yes.
