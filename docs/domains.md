# Domains

[← Back to the README](../README.md)

## Domains

A domain is one YAML file, `domains/<id>.yaml`; it is collected only when it says `enabled: true`.
`domains/example-mcp.yaml` is a complete, disabled example; `domains/examples/` holds two more (`mcp.yaml` with facet
questions, `ai-agent-harness.yaml`), also disabled and never loaded from there: copy one into `domains/` to use it. The wizard `repka domain new` asks for a name,
description, keyword groups, topics, languages, excludes, minimum stars, fork policy and scan interval, then
prints the plan and writes the file only after a final `y` (`--probe` also counts GitHub results per query).

```bash
uv run repka domain list               # id, enabled state, scan interval
uv run repka domain show example-mcp   # the validated file
uv run repka domain plan example-mcp   # the GitHub queries (keywords x languages, topics x languages) and window
```

The `profile` text is what the decider model reads, so it should state what is in scope, what is out of scope
and give examples. `min_stars` is the main lever on data volume: `domains/examples/mcp.yaml` uses 300 and a single keyword group, which keeps a
domain at a few thousand repos instead of tens of thousands. After raising it, run `repka db prune` (see
[Keeping the database small](database.md)) to drop what no longer matches.
Keep `min_stars` and the languages tight for a first run: a README keyword such as
"mcp server" matches over a thousand repositories per language even for a 5-day window at 50 stars, and the first
scan stores every hit (bounded by `collect.max_repos_per_domain_scan`, after which the run is `partial` and the next
run continues). Keyword groups search `description` and `readme` (`name` is dropped for groups with a phrase, because
GitHub then returns nothing); `in: [name]` with a phrase finds nothing either.

A domain can also carry **facet questions** under `triage.questions` (yes/no questions of `type: noul`, for example
`gateway` or `guardrails` in `domains/examples/mcp.yaml`). They never decide whether a repo is accepted (thresholds ignore
them); they say how an accepted repo differs from the others in its domain and become the facet tags of the
insights digest. Changing a domain's questions re-triages its repos at the next run, which can take a long while
on a large domain.

