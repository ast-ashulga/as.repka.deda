# Troubleshooting

[← Back to the README](../README.md)

## Troubleshooting

Start with `uv run repka doctor` and the `hints:` at the end of `uv run repka run`.

| Symptom | Cause and fix |
|---|---|
| `error: ... GitHub rejected the token` | The token expired or was revoked. Fix as printed: `gh auth login`, or replace `REPKA_GITHUB_TOKEN` in `.env`. |
| Run is `partial` with "rate limit" | GitHub search allows 30 requests a minute and the API has hourly limits. Collected data is kept; the next run continues. Narrow the domain (`min_stars`, `languages`, a shorter `first_window`) if it keeps happening. |
| Digest is empty | Normal for the first scan of a domain (silent baseline), when `triage.enabled` is false (nothing is accepted), or when nothing new happened since the last slot. See the run hints. |
| A scan of a big domain takes half an hour or more | Most of it is enrichment: every accepted repo is re-snapshotted on each scan. Set `collect.accepted_refresh_every: 20h` (for a daily scan). Check where the time went with `repka status` during a run and `repka db stats` after. |
| Many `enrich: batch of N failed ... retrying as` warnings in `logs/repka.log` | GitHub rejected heavy GraphQL batches (5xx, often 502). A failing batch is retried as two halves, and a repo that keeps failing is skipped until the next run. If it happens a lot, lower `collect.enrich_batch` (25 is safe). |
| `.state/repka.db` keeps growing | `uv run repka db prune` (dry run), then `--apply`; see [Keeping the database small](database.md). Raising a domain's `min_stars` is the lasting fix. |
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

