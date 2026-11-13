## What and why

<!-- One or two sentences. -->

## Checklist

- [ ] `uv run pytest -q` and `uv run ruff check src tests` pass
- [ ] Tests added or updated; they do not touch the network, GitHub, Telegram, Ollama or a model
- [ ] Any new feature flag defaults to `false`
- [ ] Untrusted text goes through `textutil.printable()` (terminal) or `textutil.html_escape()` (Telegram)
- [ ] No secrets, `.env`, `.sessions/`, `.state/` or logs are committed or logged
- [ ] Docs and the Unreleased section of `CHANGELOG.md` updated if behaviour changed
