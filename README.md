# repka deda

repka deda watches GitHub for repositories in technology domains you define (for example "MCP servers"),
decides locally which of them are relevant, and sends you a short Telegram digest at the times you set
(`digest.slots`, by default 08:30 and 19:30): new repos, fresh releases, star movers and dependency changes. Optionally the digest shows what is
different about each project (facet tags) and releases go in their own daily message. Everything except the
GitHub API calls and the Telegram messages stays on your Mac.

Pipeline: **collect** (GitHub search + GraphQL snapshots) -> **triage** (local decider model) ->
**embed** (local Ollama, for "similar repos") -> **digest** (Telegram). `repka run` does one pass;
launchd runs it hourly, or at fixed daily times if you prefer (see [Scheduling](#scheduling-launchd)). An optional local dashboard shows the same data in a browser.

## Requirements

- macOS on Apple Silicon (the only supported target), [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12).
- A GitHub login: `gh auth login` (repka falls back to `gh auth token`) or a personal access token in `.env`.
  A fine-grained token with read-only public access is enough.
- For triage: about 6 GB of disk for the decider model (`d1-3B`) and its environment.
- Optional: [Ollama](https://ollama.com) with `nomic-embed-text` (embeddings, `repka similar`).
- Optional: the dashboard's dependencies, `uv sync --group dashboard` (plain `uv sync` does not install them).
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
                                     # or fixed daily times: --at 22:00,09:00
```

Optional, once data is flowing: a browser view of the same data, see [Dashboard](#dashboard).

## Quick start (AI agents)

Everything is non-interactive except `domain new` and `notify login`. stdout carries results, stderr carries
progress and `error: <message>`; exit 0 ok, 1 runtime failure, 2 fixable usage or configuration error.

```bash
uv run repka doctor --json           # {ok, errors, warnings, checks[{id,status,title,fix}]}: run the `fix` of each error
uv run repka domain new --from-file brief.txt < answers.txt   # or use the Claude skill below
uv run repka domain list; uv run repka domain show <id>; uv run repka domain plan <id> --json
uv run repka run --json              # full report (stages, errors, hints, digest text) on stdout
uv run repka digest --more           # items waiting for a later digest (read-only); repka releases lists release events
uv run repka db stats                # row counts and the last runs
uv run repka db prune [--apply]      # thin old snapshots, drop repos below a domain's min_stars, shrink cached READMEs, VACUUM (dry run by default)
```

- The dashboard is for humans. Do not launch it unless asked: it is a long-running server (`uv run repka dashboard`).
  If it must run, start it in the background with a scratch `REPKA_HOME`. `doctor` and `--json` output are the agent interfaces.

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
and give examples. `min_stars` is the main lever on data volume: `domains/mcp.yaml` uses 300 and a single keyword group, which keeps a
domain at a few thousand repos instead of tens of thousands. After raising it, run `repka db prune` (see
[Keeping the database small](#keeping-the-database-small)) to drop what no longer matches.
Keep `min_stars` and the languages tight for a first run: a README keyword such as
"mcp server" matches over a thousand repositories per language even for a 5-day window at 50 stars, and the first
scan stores every hit (bounded by `collect.max_repos_per_domain_scan`, after which the run is `partial` and the next
run continues). Keyword groups search `description` and `readme` (`name` is dropped for groups with a phrase, because
GitHub then returns nothing); `in: [name]` with a phrase finds nothing either.

A domain can also carry **facet questions** under `triage.questions` (yes/no questions of `type: noul`, for example
`gateway` or `guardrails` in `domains/mcp.yaml`). They never decide whether a repo is accepted (thresholds ignore
them); they say how an accepted repo differs from the others in its domain and become the facet tags of the
insights digest. Changing a domain's questions re-triages its repos at the next run, which can take a long while
on a large domain.

## Configuration

`.config.yaml` holds all settings; every key is optional, and the defaults built into the code (the ones in the table
below and in the architecture doc) have every flag `false`. The `.config.yaml` tracked in a checkout is just a
file of overrides: on the author's machine it already switches triage, embeddings and sending on, so read it before
assuming what a run will do (`repka doctor` shows the effect). Unknown keys and wrong types are rejected at startup
naming the key. The full key list with comments is in
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
| `collect.enrich_batch` | 50 | repos per GraphQL query; if GitHub answers with many HTTP 502 (counted as retries in the run's enrich stats), lower it to 25 |
| `collect.accepted_refresh_every` | 0s (every scan) | accepted repos that the search did not match are re-snapshotted at most this often, e.g. `20h` for a daily scan. On a large domain this is what keeps a scan short and `snapshots` small; repos the search matched are always refreshed |
| `run.progress` | plain | stderr view of `repka run`: `plain`, `live`, `lines` or `jsonl` (see [Watching a run](#watching-a-run-phase-progress-eta)) |
| `digest.slots` | 08:30, 19:30 | local times at which a digest is due |
| `digest.insights` | false | richer digest items (description, kind, facet tags, legend) and overflow new/found/release items kept pending (listed by `repka digest --more`) |
| `digest.releases_message` | false | releases go in their own daily message at `digest.releases_slot` (default 09:00) |
| `digest.max_items_per_domain` | 10 | items per domain in one digest message; the rest are counted in the footer |
| `digest.facet_min_p` / `digest.pending_max_age` | 0.7 / 7d | facet tag threshold; how long an unshown item waits (both only matter with `digest.insights`) |

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

## Keeping the database small

Everything lives in `.state/repka.db`. A large domain grows it through repos that stop matching, through one
snapshot per repo per scan, and through the HTTP cache (README bodies). Since the cache keeps only the first
`triage.readme_chars` (4000) characters of a README, it no longer holds full bodies; the rest is cleaned with
`repka db prune`, which is a dry run until you pass `--apply`:

```bash
uv run repka db prune                       # dry run: exact counts, nothing changes (it rolls back)
uv run repka db prune --apply               # takes the run lock, deletes, then VACUUMs and prints the file size before/after
uv run repka db prune --apply --snapshot-days 0   # keep every snapshot (default thins snapshots older than 90 days)
```

What `--apply` removes:

- repos whose latest snapshot has fewer stars than the domain's `search.min_stars` leave that domain (with that domain's
  verdicts); repos with no snapshot stay;
- repos that no domain holds any more are deleted with their snapshots, key files, events, verdicts, embeddings and
  cached HTTP bodies; repos that appear in a recorded digest are kept so digest history stays readable;
- cached README bodies are cut to `triage.readme_chars`;
- snapshots older than `--snapshot-days` (default 90) are thinned to the last one of each ISO week per repo; a repo's
  latest snapshot and everything newer are never touched.

Run it after tightening a domain (`min_stars`, keywords) or now and then (for example monthly). Nothing prunes
automatically, and deleting a domain file does not purge its data either. Measured on a real `mcp` domain: 335 MB
to 73 MB after the first prune.

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

### Watching a run (phase, progress, ETA)

By default `repka run` prints one plain line per event on stderr. Pick a richer view with `--progress MODE` or
`run.progress` in `.config.yaml` (the default stays `plain`):

| Mode | For | What you get on stderr |
|---|---|---|
| `live` | a person at a terminal | one redrawn line: stage `2/4`, domain, `400/1200 repos 33%`, bar, elapsed, `eta ~2m40s (at 14:05)`; finished stages stay as `✓ collect 2m10s` lines (without a TTY it behaves like `lines`) |
| `lines` | `logs/launchd.log`, `tail -f`, an agent reading text | timestamped lines at every stage and domain change and at most one progress line per 10 s |
| `jsonl` | a script or agent parsing events | one JSON object per line (`plan`, `stage_start`, `step`, `progress`, `stage_end`, `note`, `log`, `summary`), nothing else; stdout is unchanged |

```bash
uv run repka run --progress live
uv run repka run --progress jsonl 2> run.events.jsonl
```

From another terminal or an agent, while a run is going (any mode, including a scheduled one):

```bash
uv run repka status          # stage, step, done/total, elapsed, eta, heartbeat and last-progress age; or how the last run ended
uv run repka status --json   # the same as JSON; state is running | idle | died | none
```

`repka status` trusts the run lock, not the file: a snapshot that says `running` while no process holds the lock
is shown as `died`. A lock held by another command (for example `repka collect`) is shown as `busy`. The
heartbeat proves the process is alive; "last progress" is the last real event, so a silent worker call is
distinguishable from a frozen process (warnings: heartbeat lost after 2 minutes, no progress after 15). The ETA is "at the current pace": it needs a few seconds and one finished unit, later stages are
estimated from your last runs (stored in `runs.stats_json`), and when a part has no estimate you see a lower bound
(`eta >2m`) or `eta ?` instead of a guess. Triage progress moves in chunks of 200 repos because the model worker
answers per chunk.

A stage that fails never stops the digest: a GitHub rate limit or a crashed decider worker makes the run `partial`
(exit 0, the next run continues); exit 1 only when every stage failed, 2 for a configuration error (for example
`--send` while `notify.enabled` is false). The summary ends with hints explaining why a run did less than expected.
Two runs never overlap, and every run is a row in `runs` (`repka db stats`). The very first run of a domain can take
a while (measured on an M-series Mac: GraphQL enrichment about 20 s per 50 repos, triage about 2 s per repo, so a
first scan of 230 repos took 8 minutes): progress on stderr (see "Watching a run") shows where it is, and an
interrupted run resumes.

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

### Insights digest and releases message (optional, off by default)

Two switches in `.config.yaml`, both `false` until you turn them on (they are meant to be used together; `repka doctor`
warns if only one is on, and neither is an error):

- `digest.insights: true`: the main digest shows only what is new or growing (🆕 🔎 📈, plus stack changes). Each
  item has a one-line description, `kind · lvl N` and up to three facet tags: the domain's extra decider answers
  with probability >= `digest.facet_min_p` (0.7). A legend line explains the icons. Items over
  `digest.max_items_per_domain` are counted in the footer (`…and 12 more: 5 🆕, 7 🔎`). New, found and release items
  stay pending until shown, or until `digest.pending_max_age` (7d) passes; stack changes and movers over the limit
  do not wait (stack changes are consumed unseen, movers are recomputed each time).
- `digest.releases_message: true`: releases (🚀) leave the main digest and go out once a day as a separate message at
  `digest.releases_slot` (09:00): per domain, newest release per repo with stars and tag, most stars first. An empty
  releases message is never sent, and a failed send keeps the releases for the next one.

```bash
uv run repka digest --more               # items waiting in the queue: new/found/release overflow, not stack/mover (read-only)
uv run repka releases                    # release events of accepted repos in the last 24h (read-only)
uv run repka releases --since 7d --domain example-mcp
```

The releases message is sent by `repka run` (not by `repka digest --send`, which delivers only the main digest). It
has its own row in the digest history, is sent at most once a day and never when empty, and the run summary has a
`releases` line. The
dashboard Home page lists both kinds of message, what is waiting and the recent releases (see
[Dashboard](#dashboard-optional)).

Without the two switches nothing changes: the digest keeps its current format and releases stay in it.

## Scheduling (launchd)

Nothing is scheduled until you ask for it. `repka schedule install` writes a launchd user agent that runs
`repka run` every hour (or, with `--at`, at fixed local times every day; a run missed while the Mac slept starts on
wake). A run only does work for domains that are due (`scan_every`), so an hourly agent is cheap; it sends digests
only when `notify.enabled` and `run.send` are both true. A run takes the run lock, so it never overlaps a manual one.

```bash
uv run repka schedule install --dry-run   # print the plist, change nothing
uv run repka schedule install             # every hour (--interval 30m to change it); replaces an old agent
uv run repka schedule install --at 22:00,09:00,12:00   # daily at these local times instead (exclusive with --interval)
uv run repka schedule status
uv run repka schedule uninstall
```

The plist holds no secrets: only `PATH` (so launchd can find `gh`; launchd's default PATH lacks `/opt/homebrew/bin`) and, when set at install time, `REPKA_HOME`. Re-run `schedule install` after moving `gh`/`uv` or changing `REPKA_HOME`. Logs go to `logs/launchd.log`.

## Dashboard (optional)

A local Streamlit view of the same database, for people. It needs the optional `dashboard` dependency group.

```bash
uv sync --group dashboard            # one time
uv run repka dashboard               # http://127.0.0.1:8501 (--port to change); Ctrl-C stops it
```

It binds to `127.0.0.1` only and sends nothing anywhere. It runs in the foreground until you stop it. Until the
database exists, the pages say to run `repka db migrate`, then `repka run`.

| Page | Shows | Notes |
|---|---|---|
| Home | Domains overview, last runs, last digests (`main` and `releases` messages), items waiting for the next digest, recent releases | Invalid domain files are reported at the top. "Waiting" is the `repka digest --more` queue and is empty and marked off until `digest.insights` or `digest.releases_message` is on; "Recent releases" is the same list as `repka releases` (24h, 7d or 30d) |
| Domains | Domains with scan status; add, edit, enable/disable, delete | Same checks as `repka domain new`; new domains start disabled unless ticked; shows the GitHub queries |
| Landscape | 2-D map of a domain's accepted repos, clusters with top terms | Needs `embed.enabled: true` and `repka embed` first; size = stars, colour = cluster, symbol = fork or original |
| Analytics | Stars over time, 7-day velocity, top movers, dependency timeline and popularity | Gaps in snapshots are shown as gaps |

The dashboard opens the database read-only (it works while `repka run` writes), with two exceptions:

- Landscape's **Recompute** stores the map in the `landscape_cache` table.
- The Domains page creates, edits and deletes `domains/<id>.yaml`. Deleting removes only the file: collected repos,
  verdicts and runs stay in the database; `repka db prune` only removes repos that a remaining domain's `min_stars` no longer matches.

Troubleshooting:

- `repka dashboard` exits 2 ("streamlit is not installed"): run `uv sync --group dashboard`.
- Landscape shows "No embeddings" or "not computed": set `embed.enabled: true`, run `uv run repka embed` (needs Ollama),
  then press Recompute. If forks are missing, the domain's `fork_policy` is `exclude`.
- Star deltas show "n/a": a 7-day delta needs a snapshot about a week old, so it fills in after about a week of
  scheduled `repka run`.

## Troubleshooting

Start with `uv run repka doctor` and the `hints:` at the end of `uv run repka run`.

| Symptom | Cause and fix |
|---|---|
| `error: ... GitHub rejected the token` | The token expired or was revoked. Fix as printed: `gh auth login`, or replace `REPKA_GITHUB_TOKEN` in `.env`. |
| Run is `partial` with "rate limit" | GitHub search allows 30 requests a minute and the API has hourly limits. Collected data is kept; the next run continues. Narrow the domain (`min_stars`, `languages`, a shorter `first_window`) if it keeps happening. |
| Digest is empty | Normal for the first scan of a domain (silent baseline), when `triage.enabled` is false (nothing is accepted), or when nothing new happened since the last slot. See the run hints. |
| A scan of a big domain takes half an hour or more | Most of it is enrichment: every accepted repo is re-snapshotted on each scan. Set `collect.accepted_refresh_every: 20h` (for a daily scan). Check where the time went with `repka status` during a run and `repka db stats` after. |
| Many `enrich: batch of N failed ... retrying as` warnings in `logs/repka.log` | GitHub rejected heavy GraphQL batches (5xx, often 502). A failing batch is retried as two halves, and a repo that keeps failing is skipped until the next run. If it happens a lot, lower `collect.enrich_batch` (25 is safe). |
| `.state/repka.db` keeps growing | `uv run repka db prune` (dry run), then `--apply`; see [Keeping the database small](#keeping-the-database-small). Raising a domain's `min_stars` is the lasting fix. |
| Domain collected 0 repos | Look at `repka domain plan <id>` and try one query with `gh api -X GET search/repositories -f q='<query> pushed:>=2026-01-01'`. Keep `min_stars` and `languages` realistic for your window. |
| `triage-venv ... is missing` / weights missing | `./deciders/setup.sh`, then `./deciders/setup.sh --download d1-3B`. |
| Run is `partial` with a worker error (`WorkerError`) | The decider process crashed or timed out (`triage.timeout_s`). The repos stay pending and the next run retries; run `repka -v triage --limit 5` to see the worker's output. |
| `Ollama is not running` | `ollama serve`, then `ollama pull nomic-embed-text`. Embedding failures make a run `partial`; digests still go out. |
| Digest not delivered | Needs `notify.enabled: true` and `run.send: true` (or `--send`), plus `repka notify login`. `repka doctor --send` lists what is missing. A failed send keeps its slot open for the next run. |
| "another repka run is in progress" | A previous run (or launchd) holds `.state/run.lock`; nothing is done and the exit code is 0. |
| Where are the logs? | `logs/repka.log` (rotating), `logs/launchd.log` for scheduled runs, `repka -v ...` prints DEBUG on stderr. |
| `repka dashboard` exits 2 ("streamlit is not installed") | The dashboard group is missing: `uv sync --group dashboard`. |
| Landscape page is empty ("No embeddings" / "not computed") | Set `embed.enabled: true`, run `uv run repka embed` (needs Ollama), then press Recompute. |

## Known limitations

- The "top movers" digest label (`/24h`) can be based on an older baseline snapshot than 24 hours when snapshots are sparse.
- Without `digest.insights`, events that stay pending (not yet sent) have no age limit: after a long pause the first digest can contain old events. With it, new, found and release items older than `digest.pending_max_age` are dropped unseen.
- If a digest is split into several Telegram messages and a later one fails after an earlier one was delivered, the slot counts as sent and the items of the failed messages are not retried.

Agent and developer notes are in [CLAUDE.md](CLAUDE.md); the plan and task status are in [wiki/](wiki/README.md).
