# Dashboard

[← Back to the README](../README.md)

## Dashboard (optional)

A local Streamlit dashboard over the same database, for people. It is a view, not a control panel: it reads the database and only two
features write anything (listed below), so it is not strictly read-only. It needs the optional `dashboard` dependency group and a source
checkout (run `uv run repka dashboard` inside the clone; the dashboard files are located relative to the package).

```bash
uv sync --group dashboard            # one time
uv run repka dashboard               # http://127.0.0.1:8501 (--port to change); Ctrl-C stops it
```

It binds to `127.0.0.1` only and sends nothing anywhere. It runs in the foreground until you stop it. Until the
database exists, the pages say to run `repka db migrate`, then `repka run`.

| Page | Shows | Notes |
|---|---|---|
| Home | Domains overview, last runs, last digests (`main` and `releases` messages), items waiting for the next digest, recent releases | Invalid domain files are reported at the top. "Waiting" is the `repka digest --more` queue and is empty and marked off until `digest.insights` or `digest.releases_message` is on; "Recent releases" is the same list as `repka releases` (24h, 7d or 30d) |
| Domains | Domains with scan status; add, edit, enable/disable, delete | Same checks as `repka domain new`; new domains start disabled unless ticked; shows the GitHub queries |
| Landscape | 2-D map of a domain's accepted repos, clusters with top terms | Needs `embed.enabled: true` and `repka embed` first; size = stars, colour = cluster, symbol = fork or original |
| Analytics | Stars over time, 7-day velocity, top movers, dependency timeline and popularity | Gaps in snapshots are shown as gaps |

The dashboard opens the database read-only (it works while `repka run` writes), with two exceptions:

- Landscape's **Recompute** stores the map in the `landscape_cache` table. For that it opens a writable connection and first
  applies any pending database migrations; nothing is written when there are no embeddings.
- The Domains page creates, edits and deletes `domains/<id>.yaml`. Deleting removes only the file: collected repos,
  verdicts and runs stay in the database; `repka db prune` only removes repos that a remaining domain's `min_stars` no longer matches.

Contributors: the dashboard tests are skipped on a plain `uv sync`; run `uv sync --group dashboard` before `uv run pytest -q`
to include them.

Troubleshooting:

- `repka dashboard` exits 2 ("streamlit is not installed"): run `uv sync --group dashboard`.
- Landscape shows "No embeddings" or "not computed": set `embed.enabled: true`, run `uv run repka embed` (needs Ollama),
  then press Recompute. If forks are missing, the domain's `fork_policy` is `exclude`.
- Star deltas show "n/a": a 7-day delta needs a snapshot about a week old, so it fills in after about a week of
  scheduled `repka run`.

