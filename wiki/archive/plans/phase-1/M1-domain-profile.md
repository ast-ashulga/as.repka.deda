# M1: Domain Profile (interview → search matrix)

[← ROADMAP](../ROADMAP.md) · Prev: [M0](M0-foundation.md) · Next: [M2 Collector](M2-collector.md)

Goal of the milestone: a domain is a validated YAML file; `repka domain plan` expands it into GitHub search queries that respect GitHub's query limits; a user can create one through an interactive CLI wizard or through a Claude Code interview skill.

Contract: [architecture § 3.3 domain file](../../../spec/architecture.md#33-domain-file-domainsidyaml) · Research: [github-api § 2](../../../research/github-api.md#2-search-what-is-and-isnt-returned)

---

### P1-T05: Domain schema, loader and `list`/`show`

**Req:** R-1.1 · **Depends on:** T01 · **Size:** S
**Goal:** Load, validate, save and delete domain files.

**Files:**
- Create: `src/repka/domain/__init__.py`, `src/repka/domain/schema.py`, `src/repka/domain/cli.py`, `domains/example-mcp.yaml`
- Modify: `src/repka/cli.py` (register `domain` subcommands)
- Test: `tests/test_domain_schema.py`

**Interfaces:**
- Consumes: `config.ConfigError`, `config.domains_dir()`, `config.parse_duration`.
- Produces (`repka.domain.schema`):
  - `@dataclass(frozen=True) SearchSpec(keywords: tuple[tuple[str, ...], ...], topics: tuple[str, ...], languages: tuple[str, ...], exclude: tuple[str, ...], min_stars: int = 0, in_fields: tuple[str, ...] = ("name", "description", "readme"), fork_policy: Literal["exclude","include","only"] = "exclude")`
  - `@dataclass(frozen=True) Domain(id: str, name: str, enabled: bool, scan_every: timedelta, profile: str, search: SearchSpec, key_files: tuple[str, ...], triage_questions: dict)` — `enabled` defaults to `False` when missing.
  - `load_domain(path: Path) -> Domain`, `load_domains(directory: Path | None = None) -> list[Domain]` (sorted by id), `get_domain(domain_id: str) -> Domain`, `dump_domain(d: Domain) -> str`, `save_domain(d: Domain, directory: Path | None = None) -> Path` (atomic tmp + `os.replace`), `delete_domain(domain_id: str, directory: Path | None = None) -> None`, `profile_hash(d: Domain) -> str` (sha256 hex of `profile`).
  - `repka.domain.cli.register(sub)` adding `domain list`, `domain show <id>`.

- [ ] **Step 1: Write failing tests**

```python
def test_roundtrip(tmp_path): d = load_domain(EXAMPLE); save_domain(d, tmp_path); assert load_domain(tmp_path / f"{d.id}.yaml") == d
def test_id_must_match_file_stem(tmp_path)          # id "a" in b.yaml -> ConfigError
def test_needs_keywords_or_topics(tmp_path)         # both empty -> ConfigError "at least one keyword group or topic"
def test_invalid_fork_policy(tmp_path)              # "maybe" -> ConfigError naming search.fork_policy
def test_scan_every_minimum_one_hour(tmp_path)      # "30m" -> ConfigError
def test_enabled_defaults_false(tmp_path)
def test_example_domain_is_valid_and_disabled():     d = load_domain(REPO/"domains/example-mcp.yaml"); assert d.enabled is False
def test_list_cli_prints_ids(home, capsys)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_domain_schema.py -v` → FAIL.
- [ ] **Step 3: Implement** `schema.py` (strict like `config.py`: unknown keys rejected; id regex `^[a-z0-9][a-z0-9-]{1,39}$`) and `domain/cli.py`.
- [ ] **Step 4: Write `domains/example-mcp.yaml`** (the example from architecture § 3.3, with `enabled: false`).
- [ ] **Step 5: Run** full suite + ruff → PASS. **Step 6: Commit & push**, report. Message: `feat(domain): domain schema, loader, list/show`.

**Acceptance criteria:** example domain loads; invalid files give `ConfigError` with the dotted key; `uv run repka domain list` prints `example-mcp  disabled  12h`.

---

### P1-T06: Query matrix and `domain plan`

**Req:** R-1.1, R-1.2a · **Depends on:** T05 · **Size:** M
**Goal:** Turn a domain into a deterministic list of GitHub search queries plus the scan window.

**Files:**
- Create: `src/repka/domain/matrix.py`
- Modify: `src/repka/domain/cli.py` (add `domain plan <id> [--json]`)
- Test: `tests/test_matrix.py`

**Interfaces:**
- Consumes: `Domain`, `SearchSpec`, `CollectConfig` (`first_window`, `overlap`).
- Produces (`repka.domain.matrix`):
  - `@dataclass(frozen=True) Query(key: str, q: str, term: str, language: str | None)` — `q` excludes the date qualifier (added per slice by the collector); `key = sha1(q).hexdigest()[:12]`.
  - `build_queries(d: Domain) -> list[Query]` — order: terms (keyword groups, then topics) × languages (`[None]` when empty).
  - `@dataclass(frozen=True) Window(field: Literal["created","pushed"], start: date, end: date)`
  - `scan_window(last_scan_at: datetime | None, now: datetime, cfg: CollectConfig) -> Window` — always `field="pushed"`; first scan: `start = now − first_window`; later: `start = last_scan_at − overlap`. (`created` stays in the Literal for ad-hoc use.)
  - constants `MAX_QUERY_CHARS = 256`, `MAX_OPERATORS = 5`.

Query composition (fixed): keyword group → `("a b" OR c)` style: multi-word keywords quoted, joined with ` OR `; topic → `topic:<t>`; then `in:<fields comma-joined>` (keyword terms only), `language:<L>` if any, `stars:>=<min_stars>` if > 0, `archived:false`, fork qualifier (`exclude` → none, `include` → `fork:true`, `only` → `fork:only`), each `exclude` word → `NOT <word>`. Operators = ORs + NOTs; if > 5 or length > 256 (with 30 chars reserved for the date qualifier), split the keyword group into chunks until each fits; a single keyword that cannot fit → `ConfigError`.

- [ ] **Step 1: Write failing tests**

```python
def test_fork_policy_qualifier():  # RF6
    assert "fork:" not in qs(policy="exclude")[0].q
    assert "fork:true" in qs(policy="include")[0].q and "fork:only" in qs(policy="only")[0].q
def test_terms_times_languages(): # 2 groups + 1 topic, 2 languages -> 6 queries, in that order
def test_no_language_qualifier_when_empty()
def test_operator_limit_splits_group(): # group of 8 keywords + 1 exclude -> every q has <= 5 OR/NOT
def test_query_length_limit(): # every len(q) <= 256 - 30
def test_keyword_too_long_raises()
def test_keys_stable_and_unique()
def test_window_first_scan_uses_pushed(): w = scan_window(None, NOW, cfg); assert (w.field, w.start) == ("pushed", (NOW - timedelta(days=365)).date())  # RF8
def test_window_incremental_uses_pushed_with_overlap()
def test_plan_json_cli(home, capsys): # prints {"domain":..., "queries":[{key,q,term,language}], "window": {...}}
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS (+ full suite, ruff).
- [ ] **Step 5: Commit & push**, report. Message: `feat(domain): query matrix and domain plan`.

**Acceptance criteria:** `uv run repka domain plan example-mcp` prints a numbered list of queries and the window; `--json` is valid JSON on stdout; all generated queries respect both limits.

---

### P1-T07: Interactive wizard `domain new`

**Req:** R-1.1 · **Depends on:** T06, T10 · **Size:** M
**Goal:** "Interview" the user in the terminal and write a valid domain file, with deterministic keyword suggestions from the description and an optional live size probe.

**Files:**
- Create: `src/repka/domain/wizard.py`, `src/repka/domain/suggest.py`
- Modify: `src/repka/domain/cli.py` (add `domain new [--from-file PATH] [--probe]`)
- Test: `tests/test_wizard.py`, `tests/test_suggest.py`

**Interfaces:**
- Consumes: `Domain`, `SearchSpec`, `save_domain`, `build_queries`.
- Produces:
  - `repka.domain.suggest.suggest_keywords(description: str, max_groups: int = 6) -> list[list[str]]` — quoted (`"..."`/`«...»`/backticks) phrases first, then the most frequent 2–3-grams without stopwords (small EN+RU stopword lists in the module), then single tokens; deterministic order; lowercase.
  - `repka.domain.wizard.WizardIO` (Protocol): `ask(prompt: str, default: str = "") -> str`, `ask_multiline(prompt: str) -> str`, `confirm(prompt: str, default: bool = False) -> bool`, `echo(text: str) -> None`. `TerminalIO` implements it with `input()`.
  - `run_wizard(io: WizardIO, description: str | None = None, probe: Callable[[str], int] | None = None) -> Domain` — interview order: name → id (slug suggested from name) → description (unless given) → keyword groups (suggestions shown, user accepts/edits as `a, b; c, d`) → topics → languages → excludes → min_stars (default 5) → fork_policy (default exclude) → scan_every (default 12h) → enable now? (default **no**) → show `build_queries` result → if `probe` given and user confirms, print `total_count` per query → confirm save.
  - The `--probe` flag wires `probe` to `GitHubClient.search_total_count` (T10) with the window qualifier appended (`pushed:>=<now − first_window>`); without a token it fails with the `ConfigError` from `github_token()`.

- [ ] **Step 1: Write failing tests**

```python
def test_suggest_quoted_phrases_first(): assert suggest_keywords('Tools for "model context protocol" servers')[0] == ["model context protocol"]
def test_suggest_skips_stopwords(): assert all(w not in {"the","and","\u0434\u043b\u044f","\u0438"} for g in suggest_keywords(TEXT) for w in g)  # "\u0434\u043b\u044f" and "\u0438" are Russian stop-words
def test_suggest_is_deterministic()
def test_wizard_scripted_creates_valid_domain(tmp_path): # ScriptedIO with answers -> Domain; save; load_domain equals it
def test_wizard_default_enabled_false()
def test_wizard_probe_called_per_query_only_when_confirmed()
def test_wizard_rejects_existing_id_unless_confirmed_overwrite(tmp_path)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Manual check:** `uv run repka domain new --from-file wiki/spikes/sample-domain.md` (write a 10-line sample description file) completes and `repka domain plan <new id>` works.
- [ ] **Step 6: Commit & push**, report. Message: `feat(domain): interactive domain wizard with keyword suggestions`.

**Acceptance criteria:** scripted test creates a valid file; wizard never enables a domain unless the user says yes; no network unless `--probe` is passed and confirmed.

---

### P1-T08: Claude Code skill `domain-profile`

**Req:** R-1.1 · **Depends on:** T06 · **Size:** S
**Goal:** A Claude Code skill that runs a richer interview (with GitHub research via `gh`) and writes/refines `domains/<id>.yaml`, using the CLI to validate.

**Files:**
- Create: `.claude/skills/domain-profile/SKILL.md`, `.claude/skills/domain-profile/domain-template.yaml`
- Test: `tests/test_skill_file.py`

**Interfaces:**
- Consumes: CLI commands `repka domain show|plan`, the domain schema (architecture § 3.3).
- Produces: a skill whose frontmatter is `name: domain-profile`, `description: Use when the user wants to define or refine a GitHub monitoring domain for repka ...`.

SKILL.md must contain, in this order:
1. **Interview** (one question at a time): what the domain is; what is in / out of scope; 3–5 known relevant repos ("anchors") and 2–3 known junk examples; languages; preferred fork policy; scan frequency.
2. **Research**: for each anchor, `gh api repos/<o>/<r> --jq '.topics,.language,.description'` to harvest topics and vocabulary; `gh api "search/topics?q=<term>" --jq '.items[].name'` for topic names.
3. **Draft** `domains/<id>.yaml` from `domain-template.yaml`; the `profile` text states inclusions, exclusions and examples, written for the deciders (plain, ≤ 1,500 chars).
4. **Validate**: `uv run repka domain show <id>`; `uv run repka domain plan <id>`.
5. **Size probe** per query: `gh api "search/repositories?q=<q>&per_page=1" --jq .total_count`; tune until each query is between ~20 and ~5,000 results and anchors are found by at least one query.
6. Leave `enabled: false` unless the user explicitly asks to enable.

- [ ] **Step 1: Write failing test** `tests/test_skill_file.py`: frontmatter has `name` and `description`; every `repka <subcommand>` mentioned in SKILL.md exists in `build_parser()`; the template loads with `load_domain` after substituting `id: template` into a file named `template.yaml`.
- [ ] **Step 2: Write** the skill and template. **Step 3: Run** → PASS.
- [ ] **Step 4: Commit & push**, report. Message: `feat(domain): Claude Code domain-profile skill`.

**Acceptance criteria:** test passes; the skill never edits files outside `domains/`; it never runs network commands other than read-only `gh api` GETs.
