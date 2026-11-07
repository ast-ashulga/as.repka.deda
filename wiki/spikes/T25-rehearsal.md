# T25 rehearsal: real GitHub, real decider, real Ollama, no Telegram

[← Wiki home](../README.md) · [ROADMAP](../plans/ROADMAP.md) · Issue [#25](https://github.com/ast-ashulga/as.repka.deda/issues/25) · Plan: [M6 P1-T25](../plans/phase-1/M6-orchestration.md)

**Verdict: the pipeline works end to end against real services up to the digest preview; one blocking bug was found and fixed.**
The Telegram delivery and the launchd agent were deliberately not exercised (human-only, see the last section).
So the plan's acceptance line "a real digest delivered once" is **not yet met**: it needs the human steps below.

## Setup

- Date 2026-11-07, macOS arm64 (MPS), `gh` login as the token source (`gh auth token`, no `.env`), Ollama 0.40.15
  with `nomic-embed-text`, decider `d1-3B` (weights already in the Hugging Face cache).
- Isolated home: `REPKA_HOME=<scratch dir>` holding its own `.config.yaml`, `domains/example-mcp.yaml` (copied, enabled
  in the scratch copy only), `.state/`, `logs/`. `deciders/` in the scratch home is a directory of symlinks to the
  checkout's tracked files plus a symlink `.venv-liquid` to a venv from another worktree (the main checkout has no
  `.venv-liquid`; `repka doctor` reported exactly that: `triage-venv environment .venv-liquid is missing -> fix: ./deciders/setup.sh`).
- Scratch `.config.yaml`: `collect.first_window: 3d`, `collect.max_repos_per_domain_scan: 500`, `triage.enabled: true`,
  `embed.enabled: true`; `notify.enabled` and `run.send` stay false. Domain: `min_stars: 10000`, languages Python and
  TypeScript, one keyword group `["model context protocol", "mcp server"]`, topic `mcp`. Nothing tracked was changed.
- `repka doctor` took 0.9 s and was all ok except the expected "sending is off: preview only" warning.

## Bug found and fixed: every keyword query returned 0 repos

First run, 5-day window, `min_stars: 50`, default `in: [name, description, readme]`: queries 1-8 (all keyword groups)
reported `0 repos`, only the topic queries found anything. The offline tests could not catch it; it is GitHub's behaviour.
Probes with `gh api -X GET search/repositories` (language Python, `stars:>=50`, `pushed:2026-11-02..2026-11-07`):

| Query (qualifiers abbreviated) | total_count |
|---|---|
| `("mcp server" OR "mcp client") in:name,description,readme` | 0 |
| same with `in:name,readme` / `in:name` / `in:name,description` | 0 |
| same with `in:readme,description` or `in:description,readme` | 1497 |
| same with `in:readme` | 1489 |
| `("mcp server") in:name,description,readme` (one phrase, parentheses) | 0 |
| `"mcp server" in:name,description,readme` (no parentheses) | 1443 |
| `(mcp OR sdk) in:name,description,readme` (words only) | 3190 |
| `(mcp-server OR mcp-client) in:name,description,readme` | 1884 |
| `("mcp server" OR mcp) in:name,description,readme` | 0 |

Rule: a parenthesised group that contains a quoted phrase and whose `in:` lists `name` matches nothing. Fix
(commit `e5d35b6`, `src/repka/domain/matrix.py`): `name` is dropped from `in:` for keyword groups that contain a phrase
(single-word groups keep it; `in: [name]` only is left as the user wrote it and still finds nothing for a phrase).
Test: `tests/test_matrix.py::test_name_field_is_dropped_only_for_groups_with_phrases`; architecture § 3 notes it.

Consequence for real domains: keyword queries now really match READMEs, about 1.4k hits per language per 5 days at 50
stars. `domains/example-mcp.yaml` (`min_stars: 5`, `first_window: 365d` by default) would hit
`max_repos_per_domain_scan` at once and need many `partial` runs plus a very long triage on a first real scan.

## Pass 1: first scan, silent baseline (run #1, `repka run --json`)

| Stage | Result | Time |
|---|---|---|
| collect | 4 queries, 5 search requests, 290 hits, 226 distinct repos, status `ok` | about 15 s |
| enrich | 226 repos in 5 GraphQL batches (`enriched 50/226` ... `226/226`) | about 85 s |
| triage | 226 decided, 34 accepted, 0 errors; two worker launches, model ready in 3.3 s and 3.9 s | about 6.5 min (about 1.7 s per repo including the README fetch) |
| embed | 34 of 34 embedded (Ollama) | seconds |
| digest | slot 19:30, `empty`: a first scan is a silent baseline, so no events | instant |
| total | `ok`, exit 0 | 495 s |

GitHub core REST calls used: about 350 (226 READMEs plus my own probes); GraphQL: 2 of 5000 budget used (5 batches); search: 5 requests
(30 per minute limit, never reached).

## Pass 2: new repos appear (run #2, `repka run --domain example-mcp --json`)

To get events without waiting a day, `Go` was added to the scratch domain's languages, so 26 repos match that were not
there before (this simulates "new repos appear"; the baseline stayed silent, so new matches emit `new_repo` events).

| Stage | Result | Time |
|---|---|---|
| collect | incremental scan, 6 queries, window `pushed:2026-11-06..2026-11-07`, 26 new repos | 13 s |
| enrich | 226 repos re-snapshotted (every repo matched again), 34 accepted ones got slow metrics, 19 events (new releases) | 83 s |
| triage | 26 decided, 7 accepted, 0 errors | 60 s |
| embed | 7 of 7 | under 1 s |
| digest | slot 19:30: preview of 10 items in 1 message, not sent | instant |
| total | `ok`, exit 0 | 156 s |

`repka digest` afterwards printed the same preview and changed nothing (`digests` and `digest_items` stayed empty).
Excerpt (public repositories, 3 of 10 lines):

```
repka · evening digest · 2026-11-07
MCP servers & tooling (example)
🔎 github/github-mcp-server ★34.3k — GitHub's official MCP Server  [tool · lvl 4 · since 2025]
🔎 googleapis/mcp-toolbox ★16.8k — MCP Toolbox for Databases is an open source MCP server for databases.  [framework · lvl 4 · since 2024]
🚀 github/github-mcp-server v2.1.0
...and 3 more
```

Triage quality note: a few accepted repos are only loosely about MCP (an API gateway, an IAM server, a database
governance tool): the thresholds `relevant 0.70, substance 2` are permissive; tuning is part of Gate G1, not a code bug.

## Pass 3 and guards

- `repka run` straight after: collect `not due until 2026-11-08T04:08:37Z`, triage and embed pending 0, run took under 1 s,
  the same digest preview (nothing was written, so it stays pending).
- `repka run --send` with `notify.enabled: false`: `error: sending is requested ... notify.enabled is false`, exit 2, before any work.
- `repka doctor --send` lists the three Telegram errors (missing `notify.enabled`, `REPKA_TG_API_ID`/`REPKA_TG_API_HASH`, session) with their fixes.

## What the human must run (not done here)

1. In the main checkout: `./deciders/setup.sh` (the venv `.venv-liquid` is missing there; the weights are already cached), then `uv run repka doctor` until it is clean.
2. Create the real domain (`uv run repka domain new`), with a high `min_stars` (for example 1000) and few languages, or
   copy `domains/example-mcp.yaml` and tighten it; keep `collect.first_window` at 30d for the rehearsal.
3. Local config (not committed): `triage.enabled: true`, `embed.enabled: true`, `notify.enabled: true`, and `run.send: true` once you trust the preview.
4. Telegram: put `REPKA_TG_API_ID` and `REPKA_TG_API_HASH` (from https://my.telegram.org) in `.env`; `uv run repka notify login`; `uv run repka notify test "hello"`; `uv run repka doctor --send` must be clean.
5. Run `uv run repka run` once, let the baseline finish (several runs if `partial`), then change nothing and run `uv run repka run --send` shortly after a slot (08:30 or 19:30) with new activity; confirm one Telegram message; run it again and confirm no second message.
6. `uv run repka schedule install --dry-run`, then `uv run repka schedule install`, `uv run repka schedule status`, `launchctl kickstart gui/$UID/com.repka.run`, check `logs/launchd.log`; `uv run repka schedule uninstall` if only a test.
7. Start the 7-day [Gate G1](../plans/ROADMAP.md#gate-g1-phase-1-is-stable).
