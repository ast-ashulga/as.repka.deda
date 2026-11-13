# Configuration

[← Back to the README](../README.md)

## Configuration

`.config.yaml` holds all settings; every key is optional, and the defaults built into the code (the ones in the table
below and in the architecture doc) have every flag `false`. The `.config.yaml` that ships with the repo lists exactly
those defaults, so a fresh clone sends nothing and runs no model until you switch something on (`repka doctor` shows
the effect of your changes). Unknown keys and wrong types are rejected at startup
naming the key. The full key list with comments is in
[the architecture spec, configuration section](../wiki/spec/architecture.md#3-configuration). Secrets live only in `.env`
(copy `.env.example`): `REPKA_GITHUB_TOKEN` (optional, empty = `gh auth token`), `REPKA_TG_API_ID`,
`REPKA_TG_API_HASH`. Logs go to `logs/repka.log`.

The switches that matter (this table is partial: it leaves out, for example, `github.*`, `embed.model`, `notify.chat_id`,
`notify.topic_id`, `triage.thresholds`, `digest.new_max_age`, `digest.star_spike_min_delta` and `run.log_level`; every key with its
default is in [the architecture spec](../wiki/spec/architecture.md#3-configuration)):

| Key | Default | Effect |
|---|---|---|
| `triage.enabled` | false | `repka run` runs the decider; without it nothing is accepted and digests stay empty |
| `embed.enabled` | false | `repka run` embeds accepted repos |
| `embed.url` | http://localhost:11434 | Ollama endpoint; must be a loopback address (`localhost`, `127.0.0.1`, `::1`), because embedding sends repo text and it must not leave the machine |
| `notify.enabled` | false | Telegram delivery is allowed |
| `run.send` | false | `repka run` delivers digests (also needs `notify.enabled`); `--send` does the same for one run |
| `collect.first_window` | 365d | backfill window of a domain's first scan |
| `collect.enrich_batch` | 50 | repos per GraphQL query; if GitHub answers with many HTTP 502 (counted as retries in the run's enrich stats), lower it to 25 |
| `collect.accepted_refresh_every` | 0s (every scan) | accepted repos that the search did not match are re-snapshotted at most this often, e.g. `20h` for a daily scan. On a large domain this is what keeps a scan short and `snapshots` small; repos the search matched are always refreshed |
| `run.progress` | plain | stderr view of `repka run`: `plain`, `live`, `lines` or `jsonl` (see [Watching a run](running.md#watching-a-run-phase-progress-eta)) |
| `digest.slots` | 08:30, 19:30 | local times at which a digest is due |
| `digest.insights` | false | richer digest items (description, kind, facet tags, legend) and overflow new/found/release items kept pending (listed by `repka digest --more`) |
| `digest.releases_message` | false | releases go in their own daily message at `digest.releases_slot` (default 09:00) |
| `digest.max_items_per_domain` | 10 | items per domain in one digest message; the rest are counted in the footer |
| `digest.facet_min_p` / `digest.pending_max_age` | 0.7 / 7d | facet tag threshold; how long an unshown item waits (both only matter with `digest.insights`) |

