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
