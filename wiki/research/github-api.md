# Research: GitHub API for the Collector

[← Wiki home](../README.md) · Related: [architecture](../spec/architecture.md)

Status: researched 2026-11-07. Items marked **(verify)** need a quick check against live docs during the task that uses them.

## 1. Limits that shape the design

| Limit | Value | Design consequence |
|---|---|---|
| Search API rate limit (authenticated) | **30 requests/min** for all search endpoints except code search; 10/min unauthenticated | Token is required. Client-side throttle defaults to 25/min to keep headroom. |
| Results per search query | **1,000** max (10 pages × 100) | Queries must be partitioned (date slices, languages) so each slice returns ≤ 1,000. |
| `incomplete_results` | `true` when the query hit GitHub's time limit; results may or may not be missing | Log it on the slice; do not treat as an error; retry the slice once with `per_page=50`. |
| Repository scope of search | Search considers up to ~4,000 matching repositories per query | Another reason for narrow slices. |
| Query string | ≤ 256 chars, ≤ 5 `AND`/`OR`/`NOT` operators **(verify)** | Query builder validates both limits and splits keyword groups. |
| Core REST rate limit | 5,000 req/hour per token | Enrichment budget: ~2–3 calls per repo per scan. |
| GraphQL | 5,000 points/hour; a 50-repo batched query costs ~1 point | Enrichment uses batched GraphQL `nodes(ids:)`. |
| Secondary rate limits | 403/429 with `Retry-After` or `x-ratelimit-remaining: 0`; triggered by bursts and concurrency | No concurrency against GitHub. On secondary limit: stop the run cleanly, keep progress, resume next run. |

## 2. Search: what is and isn't returned

- `GET /search/repositories?q=...&sort=updated&order=desc&per_page=100&page=N`.
- **Forks are excluded by default.** `fork:true` includes forks, `fork:only` returns only forks. The Phase 2 landscape map wants niche forks, so each domain has an explicit `fork_policy` (`exclude` | `include` | `only`), default `exclude`.
- Useful qualifiers: `in:name,description,readme`, `topic:x`, `language:x`, `stars:>=N`, `created:A..B`, `pushed:>=A`, `archived:false`, `is:public`, `mirror:false`.
- Pagination: follow `page` up to 10 (or `Link: rel="next"`); stop early when a page returns < per_page items.

### Partitioning strategy (decided)

1. **Matrix**: for each domain, `terms × languages`. A term is one keyword group (ORed keywords) or one topic. `languages: []` means "no language qualifier".
2. **Time window**: always `pushed:>=<since>`. Incremental scans: `since = last successful scan − 1 day` (overlap tolerates clock skew). First scan (backfill): `since = now − collect.first_window` (default 365 days) — `pushed`, not `created`, so long-established but active "main players" are found on scan 1. The first scan is a **silent baseline**: it records repos and snapshots but emits no `new_repo` events (otherwise the first digest would list hundreds of old repos as new).
3. **Bisection**: if `total_count > 1000`, split the date range in half and recurse. Stop at **1 day** granularity. If a 1-day slice still has `total_count > 1000`, take the first 1,000 (sorted by stars desc), log `slice_capped`, and move on. Never recurse below 1 day.
4. **Resume**: each completed `(domain, query_key, slice)` is recorded in `search_cursor`. A run interrupted by a rate limit skips completed slices on the next run.

## 3. Enrichment (metrics)

| Metric | Source | Notes |
|---|---|---|
| stars, forks, open issues, watchers, archived, pushedAt, primary language, topics, license, isFork + parent | GraphQL `nodes(ids:[...])` on `Repository`, 50 ids per query | Use `node_id` from search results. |
| commits last 7d / 30d | GraphQL `defaultBranchRef.target ... on Commit { history(since:) { totalCount } }` (two aliases) | Empty repos have `defaultBranchRef: null` → commits = 0. |
| latest release, releases in 90d | GraphQL `releases(first: 10, orderBy:{field:CREATED_AT, direction:DESC})` | Tags only repos have no releases; also read `refs(refPrefix:"refs/tags/", last:1)` (optional). |
| key file change | GraphQL `object(expression:"HEAD:pyproject.toml") { ... on Blob { oid } }` per key file (aliases) | **Blob OID changes ⇔ content changed.** Fetch content only when the OID differs from the stored one: `... on Blob { text }` in a second targeted query. |
| contributors count | REST `GET /repos/{o}/{r}/contributors?per_page=1&anon=true` → parse last page number from the `Link` header | Not in GraphQL. 1 call/repo; refresh at most once per 7 days per repo. Huge repos may return 403 "too large" → store `null`. |
| commit regularity | REST `GET /repos/{o}/{r}/stats/participation` → 52 weekly commit counts; `active_weeks_12 = count(nonzero in last 12)` | Returns **202** while GitHub computes stats: record `null` and retry next run; never block. Refresh at most once per 7 days. |
| README text (triage input) | REST `GET /repos/{o}/{r}/readme` with `Accept: application/vnd.github.raw+json` | Only for repos that need triage; truncate to 4,000 chars. 404 → empty. |

## 4. Identity, renames, deletions

- Key everything by **numeric `id`** (`repo_id`), not `owner/name`. Store `full_name` as a mutable attribute updated on every sighting.
- REST returns **301** for renamed/transferred repos (httpx must follow redirects for GET). GraphQL by node id is rename-proof.
- A node id that GraphQL returns as `null` (deleted / made private) → mark `repos.status = 'gone'`; keep its history.

## 5. Conditional requests

- REST responses carry `ETag`; sending `If-None-Match` gets `304 Not Modified`, which **does not count** against the core rate limit. Cache ETags for contributors/participation/readme in `http_cache(url, etag, body, fetched_at)`.
- Search responses are not worth caching (results change constantly).

## 6. Auth

- Token: `REPKA_GITHUB_TOKEN` in `.env` (gitignored) if set, otherwise the `gh` CLI token (`gh auth token`; authenticated search limit 30/min and GraphQL 5,000/h confirmed). Headers: `Authorization: Bearer <token>`, `X-GitHub-Api-Version: 2022-11-28`, a generic `User-Agent: repka-cli`.
- Token never enters decider-worker or embedder environments, logs or the DB.

## Sources

- [GitHub REST: search endpoints](https://docs.github.com/en/rest/search/search) (rate limit, 1,000-result cap, `incomplete_results`, 4,000-repo scope)
- [GitHub REST search (de)](https://docs.github.com/de/rest/reference/search)
- [Stack Overflow: getting all 1,000 search results](https://kiwix.lema.org/stackoverflow.com_en_all_2023-11/questions/74869773/how-do-i-get-all-1000-results-using-the-github-search-api) (secondary rate limits after a few pages)
- Fork default, GraphQL batching, blob OIDs, `stats/participation` 202 behaviour, conditional requests: GitHub docs, from prior knowledge **(verify)** while implementing the collector.
