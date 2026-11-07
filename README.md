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
