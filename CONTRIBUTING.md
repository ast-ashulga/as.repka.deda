# Contributing to repka

Thanks for helping. repka is a local macOS CLI that monitors GitHub repositories and sends Telegram digests;
see the [README](README.md) for what it does.

## Development setup

Requirements: macOS on Apple Silicon, [uv](https://docs.astral.sh/uv/). Python 3.12 is pinned in `.python-version`.

```bash
uv sync                          # runtime and dev dependencies
uv sync --group dashboard        # also needed to run the dashboard tests (they are skipped without it)
uv run pytest -q                 # test suite, never touches the network
uv run ruff check src tests      # lint
uv run repka --help
```

To try changes without touching your real setup, point repka at a scratch home:
`REPKA_HOME=/tmp/repka-scratch uv run repka doctor`. Note that GitHub calls can still fall back to your `gh` login.

## Conventions

- **Feature flags default to `false`** (`triage.enabled`, `embed.enabled`, `notify.enabled`, `run.send`, ...).
  A new flag must be off by default.
- **Tests never touch the network, GitHub, Telegram, Ollama or a model.** Use fixtures in `tests/fixtures/`,
  `httpx.MockTransport` and injected fakes. Tests that need the real thing are marked `@pytest.mark.live` and
  run only with `REPKA_LIVE=1`.
- **Untrusted text** (repo names, descriptions, topics, READMEs, release names) goes through `textutil.printable()`
  before terminal output and `textutil.html_escape()` before Telegram.
- **Secrets** (GitHub token, Telegram credentials, `.env`, `.sessions/`) are never committed, logged, stored in the
  database or passed to worker subprocesses.
- Repos are keyed by GitHub numeric id, never by `owner/name`.
- Config is strict: unknown keys and wrong types raise `ConfigError` naming the dotted key.
- Output: results on stdout; progress, logs and `error: ...` on stderr. Exit codes: 0 ok, 1 runtime failure,
  2 usage or configuration error.

The architecture and the full config key list are in [wiki/spec/architecture.md](wiki/spec/architecture.md).

## Pull requests

- Keep a PR focused on one change; describe what and why.
- Add or update tests; `uv run pytest -q` and `uv run ruff check src tests` must pass (CI runs both on macOS).
- Update the docs in `docs/` and [CHANGELOG.md](CHANGELOG.md) (the Unreleased section) when behaviour changes.
- Do not commit `.env`, `.sessions/`, `.state/` or `logs/`.

## Working with AI coding agents

[CLAUDE.md](CLAUDE.md) is the guide for AI agents working in this repository.

## Reporting bugs and security issues

Use the issue templates for bugs and feature requests. For vulnerabilities see [SECURITY.md](SECURITY.md).

## License

repka is released under the [MIT License](LICENSE). By submitting a contribution you agree that it is licensed under the same terms.
