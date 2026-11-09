# repka deda

repka deda watches GitHub for repositories in technology domains you define (for example "MCP servers"),
decides locally which of them are relevant, and sends you a short Telegram digest twice a day:
new repos, fresh releases, star movers and dependency changes. Everything except the GitHub API
calls and the Telegram message stays on your Mac.

Pipeline: **collect** (GitHub search + GraphQL snapshots) -> **triage** (local decider model) ->
**embed** (local Ollama, for "similar repos") -> **digest** (Telegram). `repka run` does one pass;
launchd runs it hourly.

## Requirements

- macOS on Apple Silicon (the only supported target), [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12).
- A GitHub login: `gh auth login` (repka falls back to `gh auth token`) or a personal access token in `.env`.
  A fine-grained token with read-only public access is enough.
- For triage: about 6 GB of disk for the decider model (`d1-3B`) and its environment.
- Optional: [Ollama](https://ollama.com) with `nomic-embed-text` (embeddings, `repka similar`).
- Optional: a Telegram account and API credentials from https://my.telegram.org (digest delivery).

## Quick start (humans)

Nothing is sent and nothing is scheduled until you turn it on: every feature flag is `false` by default.

```bash
uv sync
uv run repka doctor                  # lists everything that is missing, each with the exact fix
```

`doctor` reports an error until a domain exists ("no enabled domain"). Fix everything else in its list first, then create
a domain (below) and run `doctor` again until it says all good:

```bash
uv run repka domain new              # 1. describe a domain; writes domains/<id>.yaml (nothing is enabled unless you say y)
uv run repka domain plan <id>        # 2. see the GitHub queries it will run and the first-scan window
```

Turn on triage and embeddings in `.config.yaml` (flags are off by default; see [Configuration](#configuration)):

```yaml
triage: {enabled: true}   # needs ./deciders/setup.sh and ./deciders/setup.sh --download d1-3B
embed: {enabled: true}    # needs `ollama serve` and `ollama pull nomic-embed-text`
```

```bash
uv run repka doctor                  # again: it now checks the model, its environment and Ollama
uv run repka run                     # first pass: collect -> triage -> embed -> digest PREVIEW (sends nothing)
uv run repka digest                  # preview the pending digest on stdout
```

The first scan of a domain is a silent baseline, so the first preview is empty on purpose: later scans
report what is new since then. Use a short window for a first try (`collect.first_window: 7d`).
When the preview looks right, set up delivery and the schedule:

```bash
uv run repka doctor --send           # what sending needs (Telegram credentials, session, chat)
uv run repka notify login            # one time: phone, code, 2FA
uv run repka notify test "hello"     # needs notify.enabled: true
uv run repka run --send              # deliver the due slot once; run it again: nothing is sent twice
uv run repka schedule install        # hourly launchd agent (set notify.enabled and run.send to true first)
```

## Quick start (AI agents)

Everything is non-interactive except `domain new` and `notify login`. stdout carries results, stderr carries
progress and `error: <message>`; exit 0 ok, 1 runtime failure, 2 fixable usage or configuration error.

```bash
uv run repka doctor --json           # {ok, errors, warnings, checks[{id,status,title,fix}]}: run the `fix` of each error
uv run repka domain new --from-file brief.txt < answers.txt   # or use the Claude skill below
uv run repka domain list; uv run repka domain show <id>; uv run repka domain plan <id> --json
uv run repka run --json              # full report (stages, errors, hints, digest text) on stdout
uv run repka db stats                # row counts and the last runs
```

- The `domain-profile` skill (`.claude/skills/domain-profile/SKILL.md`) interviews the user, researches GitHub
  read-only and writes a validated `domains/<id>.yaml`. It may write only that file.
- Run any `repka` command in a scratch home with `REPKA_HOME=/some/dir` (config, `.env`, `domains/`, `.state/`,
  `logs/` and `deciders/` are all looked up there), which is how to rehearse without touching the real setup.
- Never enable `notify.enabled`, `run.send` or `repka schedule install` on the user's behalf, and never read
  `.env` or `.sessions/`: they hold secrets. A human decides those switches.
- Hints at the end of `repka run` (text and `hints` in `--json`) explain why a pass did less than expected.

## Domains

A domain is one YAML file, `domains/<id>.yaml`; it is collected only when it says `enabled: true`.
`domains/example-mcp.yaml` is a complete, disabled example. The wizard `repka domain new` asks for a name,
description, keyword groups, topics, languages, excludes, minimum stars, fork policy and scan interval, then
prints the plan and writes the file only after a final `y` (`--probe` also counts GitHub results per query).

```bash
uv run repka domain list               # id, enabled state, scan interval
uv run repka domain show example-mcp   # the validated file
uv run repka domain plan example-mcp   # the GitHub queries (keywords x languages, topics x languages) and window
```

The `profile` text is what the decider model reads, so it should state what is in scope, what is out of scope
and give examples. Keep `min_stars` and the languages tight for a first run: a README keyword such as
"mcp server" matches over a thousand repositories per language even for a 5-day window at 50 stars, and the first
scan stores every hit (bounded by `collect.max_repos_per_domain_scan`, after which the run is `partial` and the next
run continues). Keyword groups search `description` and `readme` (`name` is dropped for groups with a phrase, because
GitHub then returns nothing); `in: [name]` with a phrase finds nothing either.

## Configuration

`.config.yaml` (tracked defaults, every key optional, every flag `false`) holds all settings; unknown keys and
wrong types are rejected at startup naming the key. The full key list with comments is in
[wiki/spec/architecture.md § 3](wiki/spec/architecture.md#3-configuration). Secrets live only in `.env`
(copy `.env.example`): `REPKA_GITHUB_TOKEN` (optional, empty = `gh auth token`), `REPKA_TG_API_ID`,
`REPKA_TG_API_HASH`. Logs go to `logs/repka.log`.

The switches that matter:

| Key | Default | Effect |
|---|---|---|
| `triage.enabled` | false | `repka run` runs the decider; without it nothing is accepted and digests stay empty |
| `embed.enabled` | false | `repka run` embeds accepted repos |
| `notify.enabled` | false | Telegram delivery is allowed |
| `run.send` | false | `repka run` delivers digests (also needs `notify.enabled`); `--send` does the same for one run |
| `collect.first_window` | 365d | backfill window of a domain's first scan |
| `digest.slots` | 08:30, 19:30 | local times at which a digest is due |

## Collecting repositories

```bash
uv run repka collect --dry-run          # which domains are due, their windows and query counts; no network
uv run repka collect                    # search + enrich every due domain (progress on stderr)
uv run repka collect --domain ID --json # force one domain; machine-readable summary on stdout
uv run repka db stats                   # row counts afterwards
```

The first scan of a domain (window `collect.first_window`) is a silent baseline: no "new repo" events.
A GitHub rate limit stops the run cleanly (status `partial`, exit 0); what was collected is kept and the
next `repka collect` continues (a partial domain is due again at once). After a completed scan, running
it again right away reports the domain as not due.

## Triage and embeddings

Triage decides which collected repos are relevant, with a local model (set up once, see `deciders/README.md`):

```bash
./deciders/setup.sh                           # create the model environments
./deciders/setup.sh --download d1-3B          # download the weights (~6 GB) into the Hugging Face cache
uv run repka triage --dry-run                 # pending count and the first model input; loads no model
uv run repka triage --domain example-mcp --limit 20   # decide up to 20 pending repos, print a table
uv run repka triage --json                    # the same as JSON
```

Verdicts are stored; a repo is `accepted` when every threshold in `triage.thresholds` passes. A second run decides
nothing new, and a crashed worker or a rate-limited README fetch leaves the repo pending. The command runs even
while `triage.enabled` is false (that flag only governs `repka run`).

Embeddings describe accepted repos for nearest-neighbour search. They need Ollama running with the model pulled:

```bash
ollama pull nomic-embed-text
uv run repka embed                            # embed accepted repos of every enabled domain whose text changed
uv run repka embed --domain example-mcp --json
uv run repka similar owner/name -k 10         # nearest repos by stored embedding (works without Ollama)
```

## Checking readiness (`doctor`)

```bash
uv run repka doctor            # one line per check: ok / warn / error, with the exact fix
uv run repka doctor --send     # also check what sending the digest needs (Telegram credentials, session)
uv run repka doctor --json     # same, machine-readable (for scripts and agents)
```

`doctor` lists every missing piece at once: GitHub token (an expired or revoked one is an error with the fix for
where it comes from, `.env` or `gh`; one expiring within 7 days is a warning), enabled domains, decider model
(environment and downloaded weights), Ollama, Telegram. Exit 2 when something must be fixed, 0 otherwise. It reads
files only; the one network call is `GET /user` to verify the token. `repka run` performs the same checks first and
stops with exit 2 on errors before any search; warnings are printed and the run continues.

## Running everything

`repka run` is one full pass for the enabled domains: collect (domains that are due) -> triage (if
`triage.enabled`) -> embed (if `embed.enabled`) -> digest (always built, delivered only when sending is on).

```bash
uv run repka run                      # safe default: nothing is sent, the digest is printed as a preview
uv run repka run --domain example-mcp # only this domain (collected even if not due)
uv run repka run --json               # machine-readable report on stdout (progress and one summary line on stderr)
uv run repka run --send               # also deliver the digest (needs notify.enabled: true; or set run.send: true)
```

A stage that fails never stops the digest: a GitHub rate limit or a crashed decider worker makes the run `partial`
(exit 0, the next run continues); exit 1 only when every stage failed, 2 for a configuration error (for example
`--send` while `notify.enabled` is false). The summary ends with hints explaining why a run did less than expected.
Two runs never overlap, and every run is a row in `runs` (`repka db stats`). The very first run of a domain can take
a while (measured on an M-series Mac: GraphQL enrichment about 20 s per 50 repos, triage about 2 s per repo, so a
first scan of 230 repos took 8 minutes): progress lines on stderr show where it is, and an interrupted run resumes.

## Telegram (digest delivery)

Digests are sent from your own Telegram account (default chat `"me"` = Saved Messages) through
`fast-mcp-telegram`. Nothing is sent unless `notify.enabled: true`.

```bash
# REPKA_TG_API_ID / REPKA_TG_API_HASH from https://my.telegram.org go into .env
uv run repka notify login                       # one time: phone, code, 2FA
uv run repka notify test "hello" --dry-run      # prints the message, sends nothing
uv run repka notify test "hello"                # real send (needs notify.enabled: true)
uv run repka digest                             # preview of the pending digest on stdout; sends and writes nothing
uv run repka digest --slot 19:30                # preview as that slot of today
uv run repka digest --send                      # deliver the due slot and record it (needs notify.enabled: true)
```

The session in `.sessions/` is full access to the account: never commit or share it. A slot is sent at most once;
missed slots are coalesced into the latest one, and a slot older than `digest.max_lateness` is skipped. An empty
digest is never sent. A failed send is retried on the next run.

## Scheduling (launchd)

Nothing is scheduled until you ask for it. `repka schedule install` writes a launchd user agent that runs
`repka run` hourly; it sends digests only when `notify.enabled` and `run.send` are both true.

```bash
uv run repka schedule install --dry-run   # print the plist, change nothing
uv run repka schedule install             # every hour (--interval 30m to change it); replaces an old agent
uv run repka schedule status
uv run repka schedule uninstall
```

The plist holds no secrets: only `PATH` (so launchd can find `gh`; launchd's default PATH lacks `/opt/homebrew/bin`) and, when set at install time, `REPKA_HOME`. Re-run `schedule install` after moving `gh`/`uv` or changing `REPKA_HOME`. Logs go to `logs/launchd.log`.

## Dashboard (optional, read-only)

A local Streamlit view of the same database: domains, last runs and last digests. It needs the optional
`dashboard` dependency group, which plain `uv sync` does not install.

```bash
uv sync --group dashboard
uv run repka dashboard              # http://127.0.0.1:8501 (--port to change); Ctrl-C stops it
```

It binds to `127.0.0.1` only and sends nothing anywhere. It opens the database read-only, with two exceptions: the Landscape page's Recompute button stores its 2-D map in the `landscape_cache` table (needs `embed.enabled` and `repka embed` first), and the Domains page creates, edits, enables and deletes `domains/<id>.yaml` files, with the same checks as `repka domain new`; new domains start disabled.

## Troubleshooting

Start with `uv run repka doctor` and the `hints:` at the end of `uv run repka run`.

| Symptom | Cause and fix |
|---|---|
| `error: ... GitHub rejected the token` | The token expired or was revoked. Fix as printed: `gh auth login`, or replace `REPKA_GITHUB_TOKEN` in `.env`. |
| Run is `partial` with "rate limit" | GitHub search allows 30 requests a minute and the API has hourly limits. Collected data is kept; the next run continues. Narrow the domain (`min_stars`, `languages`, a shorter `first_window`) if it keeps happening. |
| Digest is empty | Normal for the first scan of a domain (silent baseline), when `triage.enabled` is false (nothing is accepted), or when nothing new happened since the last slot. See the run hints. |
| Domain collected 0 repos | Look at `repka domain plan <id>` and try one query with `gh api -X GET search/repositories -f q='<query> pushed:>=2026-01-01'`. Keep `min_stars` and `languages` realistic for your window. |
| `triage-venv ... is missing` / weights missing | `./deciders/setup.sh`, then `./deciders/setup.sh --download d1-3B`. |
| Run is `partial` with a worker error (`WorkerError`) | The decider process crashed or timed out (`triage.timeout_s`). The repos stay pending and the next run retries; run `repka -v triage --limit 5` to see the worker's output. |
| `Ollama is not running` | `ollama serve`, then `ollama pull nomic-embed-text`. Embedding failures make a run `partial`; digests still go out. |
| Digest not delivered | Needs `notify.enabled: true` and `run.send: true` (or `--send`), plus `repka notify login`. `repka doctor --send` lists what is missing. A failed send keeps its slot open for the next run. |
| "another repka run is in progress" | A previous run (or launchd) holds `.state/run.lock`; nothing is done and the exit code is 0. |
| Where are the logs? | `logs/repka.log` (rotating), `logs/launchd.log` for scheduled runs, `repka -v ...` prints DEBUG on stderr. |

## Known limitations

- The "top movers" digest label (`/24h`) can be based on an older baseline snapshot than 24 hours when snapshots are sparse.
- Events that stay pending (not yet sent) have no age limit: after a long pause the first digest can contain old events.
- If a digest is split into several Telegram messages and a later one fails after an earlier one was delivered, the slot counts as sent and the items of the failed messages are not retried.

Agent and developer notes are in [CLAUDE.md](CLAUDE.md); the plan and task status are in [wiki/](wiki/README.md).
