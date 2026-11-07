# CLAUDE.md: agent guide for repka

repka is a local macOS CLI that monitors GitHub repositories in user-defined technology domains,
triages them with local decider models and sends Telegram digests.

## Commands

- `uv sync`: install dependencies (Python 3.12, pinned in `.python-version`)
- `uv run pytest -q`: test suite (never touches the network)
- `uv run ruff check src tests`: lint
- `uv run repka --help`: CLI help; `uv run repka -v ...` adds DEBUG logs on stderr

## Where things are

- Architecture and config keys: [wiki/spec/architecture.md](wiki/spec/architecture.md)
- Plan, tasks and global constraints: [wiki/plans/ROADMAP.md](wiki/plans/ROADMAP.md)
- Task protocol (claim, commit, report): [wiki/plans/AGENT-GUIDE.md](wiki/plans/AGENT-GUIDE.md)
- Reference code to port from: `/Users/andreyshulga/projects/experimental/jevs/as.tegerun.cli`

## Global constraints

1. All feature flags default to `false`. Includes `triage.enabled`, `embed.enabled`, `notify.enabled`, `run.send`, `triage.online`, `triage.allow_remote`. Installing launchd is an explicit command, never automatic.
2. Agents commit and push themselves (project rule). Never force-push and never rewrite published history.
3. Local first: no repo data leaves the machine except GitHub API calls and the Telegram digest. No hosted model APIs unless `triage.allow_remote: true`.
4. Tests never touch the network, GitHub, Telegram, Ollama or a model. Use fixtures in `tests/fixtures/`, `httpx.MockTransport` and injected fakes. Live tests are marked `@pytest.mark.live` and run only with `REPKA_LIVE=1`.
5. Secrets (`REPKA_GITHUB_TOKEN` or `gh` login, `REPKA_TG_API_ID`/`REPKA_TG_API_HASH`, `.sessions/`) live only in `.env` / `.sessions/`. Never log them, store them in the DB or the wiki, or pass them to worker subprocesses.
6. Untrusted text (repo names, descriptions, topics, READMEs, release names): `textutil.printable()` before terminal output, `textutil.html_escape()` before Telegram.
7. Repos are keyed by GitHub numeric id, never by `owner/name`.
8. No concurrency against GitHub. Search throttle: `github.search_per_minute` (default 25).
9. macOS / Apple Silicon is the only target. Python >= 3.12 via uv. `uv.lock` is committed.
10. Model revisions are pinned to full 40-character commit hashes. Workers run offline unless `triage.online`.
11. No real-time alerts. The only outbound messages are digests at configured slots; empty digests are not sent.
12. Sub-agents run on the cheapest model that can do the job (Haiku preferred; otherwise Sonnet advised by Opus).

## Conventions

- Package layout `src/repka/`. Strict config: one `load_<section>(raw)` per section; unknown keys and wrong types raise `ConfigError` naming the dotted key.
- Output: stdout carries results (and `--json` later); stderr carries progress, logs and `error: ...`.
- Exit codes: 0 ok, 1 runtime failure (GitHub, worker), 2 usage or configuration error.
