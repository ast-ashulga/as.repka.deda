# repka configuration reference

`.config.yaml` is the source of truth: read the actual file for the current values (the defaults below are
what the tracked file ships with and can drift). Every key is optional, unknown keys and wrong types are
rejected at startup with the dotted key name, and every boolean flag defaults to `false`.
Full list with comments: `wiki/spec/architecture.md` section 3.

## `.config.yaml`

| Key | Default | Meaning |
|---|---|---|
| `github.search_per_minute` | 25 | client throttle for search (GitHub max 30); do not raise above 30 |
| `github.per_page` | 100 | page size |
| `github.timeout_s` | 30 | HTTP timeout |
| `collect.first_window` | 90d (README says 365d: check the file) | backfill window on a domain's first scan; the first scan is a silent baseline. Use `7d` for a first try |
| `collect.overlap` | 1d | incremental scans use `pushed >= last_scan - overlap` |
| `collect.max_repos_per_domain_scan` | 3000 | cap per scan; hitting it makes the run `partial` and the next run continues |
| `collect.enrich_batch` | 25 | GraphQL nodes per query |
| `collect.slow_metrics_every` | 7d | contributors/participation refresh period |
| `triage.enabled` | false | run the decider during `repka run`; off = nothing accepted, digests empty |
| `triage.model` | d1-3B | name in `deciders/models.yaml` (revision pinned to a 40-char hash) |
| `triage.timeout_s` | 1800 | worker timeout; on timeout repos stay pending |
| `triage.online` | false | allow Hugging Face downloads inside workers (leave off) |
| `triage.allow_remote` | false | permit remote decider backends (hosted); sends repo text off the machine, never enable casually |
| `triage.readme_chars` | 4000 | README characters given to the model |
| `triage.thresholds` | `{relevant: 0.70, substance: 2}` | a repo is accepted when every threshold passes; higher = stricter |
| `embed.enabled` | false | embed accepted repos during `repka run` (needs Ollama) |
| `embed.model` / `embed.url` | nomic-embed-text / http://localhost:11434 | Ollama model and endpoint |
| `digest.slots` | `["08:30", "19:30"]` | local times at which a digest is due |
| `digest.max_lateness` | 6h | a slot older than this is skipped, not sent late |
| `digest.max_items_per_domain` | 10 | digest size per domain |
| `digest.new_max_age` | 90d | first-seen repos created within this age are "new", older ones "found" |
| `digest.star_spike_min_delta` | 50 | `stars_delta_1d` threshold for the "top movers" section |
| `notify.enabled` | false | Telegram delivery is allowed at all |
| `notify.backend` | telegram_user | own account through fast-mcp-telegram |
| `notify.chat_id` | `"me"` | `"me"` = Saved Messages, or a numeric id / `@username` |
| `notify.topic_id` | null | forum topic id, if any |
| `run.send` | false | second switch: `repka run` delivers digests only if `notify.enabled` AND (`run.send` or `--send`) |
| `run.log_level` | INFO | DEBUG, INFO, WARNING or ERROR |

The two delivery switches are independent on purpose: `notify.enabled` permits sending (needed for
`notify test`, `digest --send`, `run --send`), `run.send` makes every `repka run` (including launchd) send.

## `.env` (secrets: never read or print it)

| Variable | Meaning |
|---|---|
| `REPKA_GITHUB_TOKEN` | optional; empty means `gh auth token` is used. A fine-grained read-only public token is enough |
| `REPKA_TG_API_ID`, `REPKA_TG_API_HASH` | Telegram app credentials from https://my.telegram.org; passed only to the fast-mcp-telegram subprocess |

`.sessions/` holds the Telegram session: full access to the account, never commit or share it.

## Domain file (`domains/<id>.yaml`)

Only domains with `enabled: true` are collected. `domains/example-mcp.yaml` is a complete, disabled example.

| Field | Meaning |
|---|---|
| `id` | slug, 2-40 chars of `a-z0-9-`, equals the file stem |
| `name` | human-readable |
| `enabled` | false until the user asks |
| `scan_every` | at least `1h`, default `12h` |
| `profile` | what the decider reads: in scope, out of scope, one match, one miss; at most 1,500 chars |
| `search.keywords` | list of groups; each group is its own GitHub query, words inside are ORed, quote phrases |
| `search.topics` | `topic:<t>` queries |
| `search.languages` | `[]` = any |
| `search.exclude` | each word becomes `NOT <word>` (at most 5 operators per query) |
| `search.min_stars` | keep tight for a first run |
| `search.in` | `name`, `description`, `readme` (`name` with a phrase finds nothing) |
| `search.fork_policy` | `exclude` (default), `include`, `only` |
| `key_files` | files fetched for dependency tracking, e.g. `pyproject.toml`, `package.json` |

Inspect with `uv run repka domain list|show <id>|plan <id>`.

## Paths and environment

`REPKA_HOME=<dir>` relocates `.config.yaml`, `.env`, `domains/`, `deciders/`, `.state/` (SQLite `repka.db`, `run.lock`) and `logs/`. launchd agent: `~/Library/LaunchAgents/com.repka.run.plist`, logs in `logs/launchd.log`.
