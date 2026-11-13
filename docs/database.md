# Keeping the database small

[← Back to the README](../README.md)

## Keeping the database small

Everything lives in `.state/repka.db`. A large domain grows it through repos that stop matching, through one
snapshot per repo per scan, and through the HTTP cache (README bodies). Since the cache keeps only the first
`triage.readme_chars` (4000) characters of a README, it no longer holds full bodies; the rest is cleaned with
`repka db prune`, which is a dry run until you pass `--apply`:

```bash
uv run repka db prune                       # dry run: exact counts, nothing changes (it rolls back)
uv run repka db prune --apply               # takes the run lock, deletes, then VACUUMs and prints the file size before/after
uv run repka db prune --apply --snapshot-days 0   # keep every snapshot (default thins snapshots older than 90 days)
```

What `--apply` removes:

- repos whose latest snapshot has fewer stars than the domain's `search.min_stars` leave that domain (with that domain's
  verdicts); repos with no snapshot stay;
- repos that no domain holds any more are deleted with their snapshots, key files, events, verdicts, embeddings and
  cached HTTP bodies; repos that appear in a recorded digest are kept so digest history stays readable;
- cached README bodies are cut to `triage.readme_chars`;
- snapshots older than `--snapshot-days` (default 90) are thinned to the last one of each ISO week per repo; a repo's
  latest snapshot and everything newer are never touched.

Run it after tightening a domain (`min_stars`, keywords) or now and then (for example monthly). Nothing prunes
automatically, and deleting a domain file does not purge its data either. Measured on a real `mcp` domain: 335 MB
to 73 MB after the first prune.

