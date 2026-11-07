# M5: Digest & Telegram delivery

[← ROADMAP](../ROADMAP.md) · Prev: [M4](M4-embeddings.md) · Next: [M6 Orchestration](M6-orchestration.md)

Goal of the milestone: at each due slot (morning/evening), build one structured digest of what changed in accepted repos across domains and send it to a Telegram chat through a bot. Never send empty digests, never send twice for a slot.

Research: [telegram-scheduling](../../research/telegram-scheduling.md) · Metrics definitions: [storage-embeddings § 4](../../research/storage-embeddings.md#4-derived-metrics-definitions-are-fixed-here-used-by-digest-and-dashboard) · ADR: [ADR-005](../../spec/decisions.md#adr-005-telegram-via-the-users-own-account-fast-mcp-telegram-ported-from-the-reference), [ADR-006](../../spec/decisions.md#adr-006-scheduling--hourly-launchd-pass--slot-logic-inside-the-app)

---

### P1-T19: Metrics and digest builder

**Req:** R-1.5, R-1.2b · **Depends on:** T05, T12 · **Size:** M (build fixtures from the `answers_json` contract in [architecture § 6](../../spec/architecture.md#6-core-types-srcrepkamodelspy); no dependency on T15)
**Goal:** Compute `stars_delta`/`velocity`, select and rank digest content per domain, and render it as Telegram HTML split into ≤ 4096-char messages.

**Files:**
- Create: `src/repka/store/metrics.py`, `src/repka/digest/__init__.py`, `src/repka/digest/build.py`, `src/repka/digest/render.py`
- Modify: `src/repka/models.py` (add `RepoMetrics`)
- Test: `tests/test_metrics.py`, `tests/test_digest_build.py`, `tests/test_digest_render.py`

**Interfaces:**
- Consumes: store `snapshots_for`, `events`, `verdicts`, `digest_items` tables; `Domain`; `DigestConfig`; `textutil.html_escape`, `truncate`.
- Produces:
  - `repka.store.metrics`: `stars_delta(snaps: Sequence[Snapshot], now: datetime, days: int) -> int | None` (baseline = latest snapshot with `taken_at <= now − 0.9·days`; none → `None`) · `velocity(s: Snapshot) -> float | None` (formula fixed in research § 4) · `@dataclass(frozen=True) RepoMetrics(repo_id: int, stars: int, stars_delta_1d: int | None, stars_delta_7d: int | None, stars_delta_30d: int | None, velocity: float | None)` · `repo_metrics(conn, repo_id: int, now: datetime) -> RepoMetrics | None`.
  - `repka.digest.build`: `@dataclass DigestSection(domain_id: str, domain_name: str, new_repos: list[DigestItem], releases: list[DigestItem], movers: list[DigestItem], stack_changes: list[DigestItem], more: int)` · `@dataclass Digest(slot: str, local_date: str, sections: list[DigestSection], shown_event_ids: list[int], consumed_event_ids: list[int])` with property `item_count: int` · `build_digest(conn, domains: list[Domain], now: datetime, cfg: DigestConfig, slot: str, local_date: str) -> Digest`.
  - `repka.digest.render`: `slot_label(slot: str) -> str` (`"morning"` if hour < 12 else `"evening"`) · `render_digest(d: Digest) -> list[str]` (lines) · `split_messages(lines: list[str], limit: int = 4096) -> list[str]`.

Selection rules (fixed):
- Accepted = the repo's latest verdict for that domain has `accepted=1`.
- Events with `digest_id IS NULL`: of accepted repos → shown; of repos with a rejecting verdict → `consumed` silently; of repos without any verdict yet → left pending (appear after triage).
- `key_file_changed` is shown only via its `stack_changed` sibling (same repo, path, run); a bare `key_file_changed` is consumed silently.
- Movers: accepted repos with `stars_delta_1d >= cfg.star_spike_min_delta`, excluding repos shown as `mover` in `digest_items` within the last 24 h.
- Per domain cap `cfg.max_items_per_domain` filled in priority order new → releases → movers → stack; overflow events are consumed and counted in `more`.
- `new_repo` events: repo `created_at` within `cfg.new_max_age` → shown as 🆕 *new*; older → shown as 🔎 *found* (first seen by repka, but not a new project) (RF8).
- Ranking: new/found by (`substance.pred`, `relevant.p`, stars) desc; releases by stars desc; movers by `stars_delta_1d` desc; stack by stars desc.

Render format (fixed; all repo text `html_escape`d, descriptions truncated to 140 chars):

```
<b>repka</b> · morning digest · 2026-11-07

<b>MCP servers &amp; tooling</b>
🆕 <a href="https://github.com/o/r">o/r</a> ★1.2k — description… <i>tool · lvl 3</i>
🔎 <a href="...">o/r</a> ★8.4k — description… <i>library · lvl 4 · since 2023</i>
🚀 <a href="...">o/r</a> <code>v1.2.0</code>
📈 <a href="...">o/r</a> +340★/24h · velocity 3.1
🧩 <a href="...">o/r</a> pyproject.toml +fastmcp −mcp
…and 4 more
```

- [ ] **Step 1: Write failing tests**

```python
# test_metrics.py
def test_stars_delta_null_without_baseline()            # RF4: one snapshot -> None for 1/7/30
def test_stars_delta_uses_latest_snapshot_older_than_0_9_days()
def test_velocity_formula(): assert velocity(S(commits_30d=20, active_weeks_12=6, releases_90d=2)) == round(math.log1p(20)*(0.5+6/24)+0.5*math.log1p(2), 2)
def test_velocity_all_null_is_none()
# test_digest_build.py
def test_rejected_events_consumed_untriaged_pending()
def test_bare_key_file_change_consumed_stack_change_shown()
def test_cap_and_more_counter()
def test_mover_not_repeated_within_24h()
def test_new_repo_ranking()
def test_old_repo_found_later_is_marked_found_not_new()   # RF8
def test_empty_when_nothing_new(): assert build_digest(...).item_count == 0
# test_digest_render.py
def test_html_escaped(): # description "<script>&" -> "&lt;script&gt;&amp;"
def test_split_never_exceeds_limit_or_breaks_lines()
def test_slot_label()
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit & push**, report. Message: `feat(digest): metrics, digest selection and rendering`.

**Acceptance criteria:** RF4 pinned; rendering is valid Telegram HTML (only `b i a code` tags); the builder writes nothing (pure read).

---

### P1-T20: Telegram sender (user account via fast-mcp-telegram)

**Req:** R-1.5 · **Depends on:** T01 · **Size:** M
**Goal:** Send HTML message parts from the user's own Telegram account to one configured chat (default `"me"` = Saved Messages, optional forum `topic_id`), by porting the reference's guarded MCP sender; plus the one-time interactive login.

**Files:**
- Create: `src/repka/notify/__init__.py`, `src/repka/notify/mcp_client.py`, `src/repka/notify/telegram_user.py`, `src/repka/notify/cli.py`
- Modify: `src/repka/cli.py`, `pyproject.toml` (deps `mcp>=1.25`, `fast-mcp-telegram>=0.46,<0.47`; dev `pytest-asyncio>=0.24`)
- Test: `tests/test_telegram_user.py`

**Interfaces:**
- Consumes: `NotifyConfig`, `config.telegram_credentials()`, `config.session_dir()`, `config.session_file()`, `config.telegram_server_env()`, `config.venv_script()`, `ConfigError`. Port from the reference: `src/tegerun/mcp_client.py` (whole: `GuardedSession`, `open_session`, timeouts, server log 0600) and `src/tegerun/notifier.py` (`deliver`, `_send_error`, `Sender` lifecycle); `extract_payload` from `src/tegerun/reader.py`.
- Produces:
  - `repka.notify.mcp_client`: `SEND_TOOLS = frozenset({"send_message"})` (the **only** allowlist in this project: repka never reads chats) · `class ToolNotAllowed(Exception)` · `GuardedSession(session, allow, timeout=120.0)` · `open_session(*, allow) -> AsyncContextManager[GuardedSession]` (server = `venv_script("fast-mcp-telegram")`, `env=telegram_server_env()`, `cwd=session_dir()` so the server never auto-loads repka's `.env`; requires the session file).
  - `repka.notify.telegram_user`:
    - `@dataclass SendResult(ok: bool, sent: int, message_ids: list[int], error: str | None)`.
    - `class Sender(Protocol)`: `send(parts: list[str]) -> SendResult` — **synchronous** (T21 calls it from sync code).
    - `async def deliver_parts(session, chat_id: str, parts: list[str], topic_id: int | None) -> SendResult` — one `send_message` per part with `parse_mode="html"`, `reply_to_id=topic_id` when set; stops at the first failed part; never raises.
    - `TelegramUserSender(chat_id: str, topic_id: int | None = None, *, session_factory: Callable[[], AsyncContextManager] | None = None)` — `send(parts)` runs `asyncio.run(...)` around one MCP session for all parts (default factory = `open_session(allow=SEND_TOOLS)`); any exception → `SendResult(ok=False, …)`.
  - `repka.config` additions (owned by this task): `telegram_credentials() -> TelegramCredentials` (`api_id`, `api_hash` from `.env` keys `REPKA_TG_API_ID`/`REPKA_TG_API_HASH`; `__repr__` hides both; `ConfigError` when empty) · `session_dir() -> Path` (`<root>/.sessions`, created 0700) · `session_file() -> Path` (`.sessions/telegram.session`) · `require_session() -> Path` (`ConfigError` with the hint `uv run repka notify login`) · `telegram_server_env() -> dict[str, str]` (`PATH, HOME, LANG, LC_ALL, TMPDIR` + `API_ID, API_HASH, SESSION_DIR, SESSION_NAME=telegram, SERVER_MODE=stdio, DO_NOT_TRACK=1`; **no** GitHub token) · `venv_script(name) -> str`.
  - CLI: `repka notify login` (interactive one-time login: `os.execve` of `fast-mcp-telegram-setup` with `telegram_server_env()`, cwd `session_dir()`; phone, code, 2FA go to the real terminal) · `repka notify test "<text>" [--dry-run]` (real send only if `notify.enabled: true`, else `ConfigError` with the hint; `--dry-run` prints the parts and opens no session).

- [ ] **Step 1: Write failing tests** (a `FakeSession` with async `call_tool` recording calls and returning scripted payloads; no subprocess, no network):

```python
def test_send_posts_html_parts_in_order()             # parse_mode="html" on every call
def test_topic_id_sent_as_reply_to_id()
def test_chat_id_me_passed_through()
def test_first_failed_part_stops_delivery()           # 3 parts, part 2 fails -> sent == 1, ok False
def test_exception_in_session_becomes_send_result()   # never raises
def test_guarded_session_rejects_other_tools()        # call_tool("get_messages") -> ToolNotAllowed, nothing forwarded
def test_server_env_has_no_github_token(monkeypatch)
def test_credentials_repr_hides_values()
def test_missing_session_hint_mentions_notify_login(home)
def test_notify_test_refused_when_disabled(home)      # exit 2
def test_notify_test_dry_run_prints_without_session(home, capsys)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Manual check (human):** put `REPKA_TG_API_ID` / `REPKA_TG_API_HASH` in `.env` (already done by the user), `uv run repka notify login` (own session; do **not** copy the reference's `.sessions/telegram.session`, two processes on one session file conflict), keep `notify.chat_id: "me"`, enable `notify.enabled` locally, `uv run repka notify test "hello"` → message appears in Saved Messages.
- [ ] **Step 6: Commit & push**, report. Message: `feat(notify): telegram user-account sender via fast-mcp-telegram`.

**Acceptance criteria:** nothing is sent unless `notify.enabled: true`; only `send_message` can reach the server; credentials and session never printed, logged or committed; the sender never raises.

---

### P1-T21: Digest slots and `repka digest`

**Req:** R-1.5, R-G3 · **Depends on:** T19, T20 · **Size:** M
**Goal:** Decide whether a slot is due (with coalescing and lateness), build/send/record exactly one digest per slot, and never send empty ones.

**Files:**
- Create: `src/repka/digest/slots.py`, `src/repka/digest/run.py`, `src/repka/digest/cli.py`
- Modify: `src/repka/cli.py`
- Test: `tests/test_digest_slots.py`, `tests/test_digest_run.py`

**Interfaces:**
- Consumes: `build_digest`, `render_digest`, `split_messages`, `Sender`, `DigestConfig`, `NotifyConfig`, `RunConfig`; tables `digests`, `digest_items`, `events`.
- Produces:
  - `repka.digest.slots`: `@dataclass(frozen=True) SlotDecision(due: tuple[str, str] | None, skipped: list[tuple[str, str]])` (pairs are `(slot "HH:MM", local_date "YYYY-MM-DD")`) · `decide_slot(slots: list[str], now_local: datetime, recorded: set[tuple[str, str]], max_lateness: timedelta) -> SlotDecision` — candidates are today's and yesterday's slots with `slot_time <= now_local` not in `recorded`; the latest candidate within `max_lateness` is due; all other candidates are skipped · `recorded_slots(conn, since_date: str) -> set[tuple[str, str]]`.
  - `repka.digest.run`: `@dataclass DigestOutcome(status: Literal["not_due","empty","preview","sent","failed"], slot: str | None, local_date: str | None, item_count: int, parts: list[str], error: str | None = None)` · `run_digest(conn, domains: list[Domain], cfg: AppConfig, sender: Sender | None, now: datetime, send: bool, slot: str | None = None, tz: ZoneInfo | None = None) -> DigestOutcome` — `now` must be timezone-aware UTC (`ValueError` otherwise); local time = `now.astimezone(tz)`, `tz=None` → system zone. **Tests always pass an explicit `tz`** (e.g. `ZoneInfo("Asia/Tbilisi")`) so they pass on any machine.
  - Behaviour (fixed): `send=False` → never writes anything (preview). With `send=True`: record skipped slots as `skipped`; empty digest → record `empty`, send nothing; send OK (≥ 1 part delivered) → in one transaction: insert `digests` row `sent`, set `events.digest_id` for shown + consumed events, insert `digest_items`; send failure with 0 parts delivered → record nothing (the slot stays open and is retried next hourly run while within `max_lateness`), return `failed`. `slot` argument forces that slot for today's date (still refuses if already recorded).
  - CLI `repka digest [--slot HH:MM] [--send] [--dry-run]`: default and `--dry-run` = preview to stdout; `--send` requires `notify.enabled: true` (else `ConfigError`, exit 2).

- [ ] **Step 1: Write failing tests**

```python
def test_slot_not_due_before_first_slot()
def test_missed_slots_coalesce()             # RF5: now 20:00, nothing recorded -> due 19:30, skipped 08:30
def test_stale_slot_skipped()                # RF5: now 03:00, yesterday 19:30 is 7.5h late (> 6h) -> nothing due, it is skipped
def test_yesterday_evening_after_midnight_within_lateness()   # now 00:30 -> due (19:30, yesterday)
def test_empty_digest_not_sent()             # RF5: sender.send never called, row status 'empty'
def test_preview_changes_no_state()
def test_send_marks_events_and_records_items()
def test_send_failure_leaves_slot_open_for_retry()
def test_no_double_digest_same_slot()        # second run_digest in same slot -> not_due
def test_send_refused_when_notify_disabled(home)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Manual check:** `uv run repka digest` (preview) after T13/T15 manual runs; paste the rendered output into the report.
- [ ] **Step 6: Commit & push**, report. Message: `feat(digest): slots, coalescing and repka digest`.

**Acceptance criteria:** RF5 cases pinned by tests; preview never writes; a failed send is retried next run, a successful one never repeated.
