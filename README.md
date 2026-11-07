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
