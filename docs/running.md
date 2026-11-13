# Running repka

[← Back to the README](../README.md)

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

