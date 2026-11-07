# as.repka.deda

CLI tool for local monitoring, Deciders-based triage, and trendwatching of GitHub repositories.

## Install

Requires macOS on Apple Silicon. [uv](https://docs.astral.sh/uv/) fetches Python 3.12 automatically.

```bash
uv sync
cp .env.example .env   # optional: fill in REPKA_GITHUB_TOKEN, or log in with `gh auth login`
uv run repka --help
```

Settings live in `.config.yaml` (every key optional, every flag off by default). Agent and
developer notes are in [CLAUDE.md](CLAUDE.md).

## Collecting repositories

A domain is collected only when `enabled: true` in `domains/<id>.yaml`. The GitHub token comes from
`REPKA_GITHUB_TOKEN` in `.env`, or from `gh auth login`.

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

## Telegram (digest delivery)

Digests are sent from your own Telegram account (default chat `"me"` = Saved Messages) through
`fast-mcp-telegram`. Nothing is sent unless `notify.enabled: true`.

```bash
# REPKA_TG_API_ID / REPKA_TG_API_HASH from https://my.telegram.org go into .env
uv run repka notify login                       # one time: phone, code, 2FA
uv run repka notify test "hello" --dry-run      # prints the message, sends nothing
uv run repka notify test "hello"                # real send (needs notify.enabled: true)
```

The session in `.sessions/` is full access to the account: never commit or share it.

```bash
uv run repka digest                  # preview of the pending digest on stdout; sends and writes nothing
uv run repka digest --slot 19:30     # preview as that slot of today
uv run repka digest --send           # deliver the due slot and record it (needs notify.enabled: true)
```

A slot is sent at most once; missed slots are coalesced into the latest one, and a slot older than
`digest.max_lateness` is skipped. An empty digest is never sent. A failed send is retried on the next run.

Triage decides which collected repos are relevant (needs the local decider environment, see `deciders/README.md`):

```bash
uv run repka triage --dry-run                 # pending count and the first model input; loads no model
uv run repka triage --domain example-mcp --limit 20   # decide up to 20 pending repos, print a table
uv run repka triage --json                    # the same as JSON
```

Verdicts are stored; a repo is `accepted` when every threshold in `triage.thresholds` passes. A second run decides
nothing new, and a crashed worker or a rate-limited README fetch leaves the repo pending. The command runs even
while `triage.enabled` is false (that flag only governs `repka run`).

Embeddings describe accepted repos for nearest-neighbour search. They need Ollama running with the model pulled
(`ollama pull nomic-embed-text`):

```bash
uv run repka embed                            # embed accepted repos of every enabled domain whose text changed
uv run repka embed --domain example-mcp --json
uv run repka similar owner/name -k 10         # nearest repos by stored embedding (works without Ollama)
uv run repka similar owner/name --domain example-mcp
```

If Ollama is down, `repka embed` exits 1 with a hint to start it; a missing model exits 2 with the `ollama pull`
command. `embed.enabled` is false by default and only governs `repka run`.

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
`--send` while `notify.enabled` is false). The summary ends with hints explaining why a run did less than expected
(triage disabled, nothing due, preview only). Two runs never overlap, and every run is a row in `runs`
(`repka db stats`). The very first run of a domain can take a long time (GraphQL enrichment is about 20 s per 50
repos): progress lines on stderr show where it is, and an interrupted run resumes.
