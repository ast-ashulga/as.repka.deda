# repka troubleshooting

Order of investigation: `uv run repka doctor` (it lists every missing piece with its fix), then the `hints:`
at the end of `uv run repka run`, then `logs/repka.log` (`logs/launchd.log` for scheduled runs), then
`uv run repka -v <command>` for DEBUG output. Exit codes: 0 ok (including `partial`), 1 runtime failure,
2 configuration or usage error.

| Symptom | Cause and fix |
|---|---|
| `GitHub rejected the token` | Token expired or revoked. Fix as `doctor` prints: `gh auth login`, or the user replaces `REPKA_GITHUB_TOKEN` in `.env` themselves. A token expiring within 7 days is only a warning |
| Run is `partial` with "rate limit" | GitHub search allows 30 requests a minute plus hourly API limits. Collected data is kept and the next run continues. If it keeps happening, narrow the domain: higher `min_stars`, fewer `languages`, shorter `collect.first_window` |
| Digest is empty | Normal when: the domain's first scan (silent baseline), `triage.enabled` is false (nothing accepted), nothing new since the last slot, or the slot's digest was already sent. Empty digests are never sent. Check the run hints |
| Domain collected 0 repos | `uv run repka domain plan <id>` and try one query by hand: `gh api -X GET search/repositories -f q='<query> pushed:>=2026-01-01'`. `min_stars` or `languages` are probably unrealistic for the window |
| Domain collected thousands of repos | Keywords too broad (a README keyword like "mcp server" matches over 1,000 repos per language). Tighten `min_stars`/`languages`; the scan stops at `collect.max_repos_per_domain_scan` and continues next run |
| `triage-venv ... is missing` / weights missing | `./deciders/setup.sh`, then `./deciders/setup.sh --download d1-3B` (about 6 GB) |
| Run `partial` with `WorkerError` | The decider process crashed or hit `triage.timeout_s`. Repos stay pending and the next run retries. Run `uv run repka -v triage --limit 5` to see the worker output |
| Nothing accepted although repos were collected | Triage is off or has not run: `uv run repka triage --dry-run` shows the pending count. Thresholds too strict? Compare with `triage.thresholds` |
| `Ollama is not running` | `ollama serve` in a terminal, then `ollama pull nomic-embed-text`. Embedding failures make a run `partial`; digests still go out |
| Digest not delivered | Needs `notify.enabled: true` and (`run.send: true` or `--send`), plus a Telegram login (`! uv run repka notify login`). `uv run repka doctor --send` lists what is missing. A failed send keeps its slot open and is retried on the next run |
| `--send` fails with exit 2 | `notify.enabled` is false; sending needs it |
| "another repka run is in progress" | A previous run (or launchd) holds `.state/run.lock`; nothing was done and the exit code is 0. Check `pgrep -fl "repka run"`. If nothing is running the lock is stale-free (the OS releases it when a process dies), so just retry |
| Scheduled runs do nothing | `uv run repka schedule status` (loaded? last exit code), `logs/launchd.log`. launchd's PATH lacks `/opt/homebrew/bin`, so after moving `gh`/`uv` or changing `REPKA_HOME` re-run `repka schedule install` (needs the user's yes). The agent does not run at login, only on its interval |
| Config error at startup (exit 2) | The message names the dotted key; fix that key. Unknown keys and wrong types are rejected |
| Old events in the first digest after a pause | Known limitation: pending events have no age limit |
| Digest split into several messages, later one failed | Known limitation: the slot counts as sent and the failed messages' items are not retried |
| "Top movers /24h" looks off | Known limitation: it can use an older baseline snapshot when snapshots are sparse |
