# Digest with insights: facet tags and a separate releases message

Status: implemented (2026-11-10); the flags `digest.insights` and `digest.releases_message` default to `false`; enabling them in the real `.config.yaml` awaits the owner's decision. Author: Claude, after a brainstorm with the owner.
Everything marked "my decision" was not stated by the owner; check it when reading.

## Why

The owner reads the whole Telegram digest and still does not understand what is behind a line. The goal:
find out which technologies and ideas **emerge, get implemented and gain strength**, and understand
**how a project differs from others in its class**.

Today:

- `🚀` releases take the whole limit of 10 items per domain (shared by all sections), so new and growing projects
  are pushed out. Releases are sorted by stars only.
- A release line is `owner/name` + tag. There is no description, no stars and no Decider answers, so the name alone
  does not tell what the project is.
- "…and N more" are the items that did not fit the limit. They are marked as processed and **never appear anywhere else**.
- The icons (`🚀 🆕 🔎 📈`) are not explained anywhere. The order of the lines cannot be explained.

Success: new and growing projects are visible within seconds; for each project it is visible what is unusual about it;
releases are not lost but do not get in the way.

## Goals

1. The main digest (slot 08:30, as now) shows **only new and growing**: 🆕, 🔎, 📈
   (and stack changes, see "Left to the builder").
2. Each item has: a one-line description, stars, `kind · lvl`, **facet tags**.
3. A separate Telegram message **once a day** with releases (this is the owner's choice).
4. An icon legend in the message. "…and N more" is replaced by a per-type counter and a command for the full list.
5. Facet tags are built from the Decider's answers to the domain's questions (`triage.questions`).

## Non-goals and anti-goals

- Do not break repka: existing behaviour is preserved until the new one is enabled.
- No real-time notifications (global constraint 11 of the project): releases go once a day on a schedule.
- No model-written project summary (an LLM summary): only the fixed Decider questions.
- No cloud models (`triage.allow_remote` stays `false`). Data does not leave the machine except to GitHub and Telegram.
- Do not change `triage.thresholds` or the `accepted` criteria: facet questions do not affect acceptance.
- Anti-goal: tags that every project has (noise). That is worse than having no tags.

## Decisions taken

**Class.** A class is a domain: a repository belongs to the class if the Decider answered "yes" to `relevant`
against the domain's `profile`. Differences within the class are expressed by the answers to additional questions.

**Facet questions (already written into `domains/*.yaml`, 5 per domain, type `noul`):**

- `ai-agent-harness`: `self_modifies`, `uses_own_models`, `trains_weights`, `sandbox`, `subagents`.
- `mcp`: `remote_transport`, `gateway`, `guardrails`, `token_efficiency`, `wraps_service`.

A trial run on 30 accepted repositories (a copy of `REPKA_HOME`, no Telegram, the database was not changed):

- they discriminate well: `self_modifies`, `trains_weights`, `subagents`, `gateway`, `guardrails`, `wraps_service`;
- they discriminate weakly or are useless (almost everyone gets the same answer): `sandbox`, `uses_own_models`, `remote_transport`, `token_efficiency`.

The owner decided to keep all 10 questions and to **judge them by the results**. Weak questions can be replaced or removed
once re-triage has accumulated data (see "Evaluation").

**Main digest (the mock-up is agreed).**

- Sections per domain, 🆕 / 🔎 / 📈 inside. No releases in it.
- Under the project name: a one-line description (truncated), then `kind · lvl N` and the facet tags.
- A tag is shown if `p >= digest.facet_min_p` (my decision: 0.7; a configurable key).
- At most 3 tags per project, those with the highest `p` (my decision).
- A one-line icon legend at the bottom.
- Instead of "…and N more": a per-type counter, for example "…and 12 more: 5 🆕, 7 🔎", plus a command for the full list
  (`repka digest --more`, my decision on the name). For this, the items that did not fit the limit **must not be lost**:
  they are not marked as processed until they are shown (today they are marked immediately; this is a change
  of semantics, do it deliberately, with a test).

**Releases message (the mock-up is agreed).**

- A separate message once a day, in its own slot (a key like `digest.releases_slot`, my decision on the name).
- Sections per domain: "N releases", for each project `★stars · tag`, sorted by stars, a line limit per domain,
  then "…and N more".
- No facet tags in releases.
- Command for the full list: `repka releases --since 24h` (my decision on the name).
- An empty message is not sent (global constraint 11).

**Flags.** Everything new is switched on by flags in `.config.yaml`, **`false` by default** (the global rule of the owner
and of the project). Proposal (my decision): `digest.insights: false` (the new format of the main digest without releases)
and `digest.releases_message: false`. While the flags are off, the digest stays as it is today.

## Semantics refined after review (all of this is my decision)

**Accounting for sent releases.** The releases message is a separate record in `digests` (type `releases`, key
`(kind, slot, local_date)`, a schema migration is needed, as for the landscape-cache migration: `migrate()` must stay safe under a
race). `release` events are marked as sent **only after this message was sent successfully**, in the same
transaction as the record. If sending fails or the slot is skipped, the events stay in the queue and go into the
next message: releases are neither lost nor duplicated.

**Who consumes `release` events.** If `digest.releases_message: true`, the main digest neither shows nor consumes releases,
regardless of `digest.insights`: the events belong to the releases message. If
`releases_message: false`, the behaviour of releases in the main digest does not change (as today).

**Flag combinations** (both default to `false`):

| `insights` | `releases_message` | Result |
|---|---|---|
| false | false | as today, nothing changes |
| true | false | new format of the main digest; releases stay in it as today, at the end of the section and inside the shared limit (otherwise they would be lost) |
| false | true | old format of the main digest, but without releases; releases go out as a separate message |
| true | true | the target configuration from the mock-ups |

`repka doctor` warns if only one of the flags is on (this is not an error).

**Items not shown (`…and N more`).** Overflow items 🆕/🔎/📈 stay in the queue until they are shown.
So that the queue does not grow without bound and old "novelties" do not resurface, `digest.pending_max_age`
is introduced (default `7d`): an item older than this age is silently marked as processed without being shown. This applies only
when `digest.insights: true`; with `false` the behaviour is unchanged (overflow is marked as processed immediately).

**Commands.** `repka digest --more` and `repka releases --since 24h` only read and do not mark anything
as processed. `--more` prints the items in the queue that were not shown (not just those of the last sent digest), with the same
tags. `releases --since` reads the `events` table (`kind = release`), whether or not the event
was sent. The builder checks that events are not deleted earlier than the `--since` window needs; if they are,
the builder resolves the retention question and tells the owner.

**Releases slot.** `digest.releases_slot` defaults to `"09:00"` (local time, configurable). A missed slot
is subject to the same `digest.max_lateness` as the regular slots: if it is older, the message is skipped and the events
stay in the queue and go out in the next slot (so on such a day the "last 24 hours" window may include more). This is not
a loss: the releases stay in the queue.

**Owner's decision about YAML.** The questions in `domains/*.yaml` stay; re-triage of 9519 repositories at the next
scheduled run is accepted and does not depend on the flags. Evaluation by results (section below).

## Deviations of the implementation from the specification

- **Releases record key.** Instead of a unique key `(kind, slot, local_date)` the key stays `(slot, local_date)`.
  The releases message record stores `slot = 'releases:HH:MM'` and `kind = 'releases'` (column `digests.kind`,
  migration 004, default `main`); the main record stores the plain slot `HH:MM`. Reason: `events` and `digest_items`
  reference `digests`, and rebuilding the table for a new key is not justified. The prefix rule:
  `digest/slots.py` `slot_key(kind, slot)`, `recorded_slots(kind=...)` strips the prefix.
- **The pre-release label is dropped.** The payload of the `release` event (`tag`, `name`, `published_at`, `url`) has no such field,
  so the message does not show it (as was agreed for the "no data" case).
- **Overflow by type.** With `insights: true`, items of `new`/`found`/`release` that were not shown stay in the queue (up to
  `pending_max_age`, the same age limit for `new_repo` and `release`); only the unshown `stack` items are still marked
  as processed. Reason for `release`: the row of the flag table "releases stay in the main digest", otherwise they
  would be lost daily (releases go last and share the domain limit with the new group). `repka digest --more` prints
  the queue (an unlimited build, only `new`/`found`/`release`), not the difference from the regular build. Movers (📈) are not
  tied to events and have no queue.
- **Releases window.** `repka releases` reads `events.kind = 'release'` by `detected_at`; events are not deleted
  earlier than the window needs, so no separate retention decision was required.

## Constraints

- All the global constraints of the project's `CLAUDE.md`: network only to GitHub and Telegram, secrets only in `.env`,
  `textutil.printable()` for the terminal and `textutil.html_escape()` for Telegram (repository descriptions are
  untrusted text), repositories are identified by numeric id.
- Tests do not touch the network; fixtures in `tests/fixtures/`, fakes through `pipeline.Boundaries`.
- A single pass must not fail because facet answers are missing: a repository without answers is printed without tags.
- The answers live in `verdicts.answers_json`; the latest verdict of the domain by `decided_at` is taken
  (as `_latest_verdicts` in `src/repka/digest/build.py` does).
- Telegram length limits are already handled by splitting into parts (`parts`); new messages must fit the same mechanism.
- Commits only with the owner's approval (the user's global rule). The project rule that lets agents
  commit and push was not separately confirmed here.

## State at the time of writing

- The questions were written into the author's own domain files `domains/ai-agent-harness.yaml` and `domains/mcp.yaml`. Those files are local to each user and not tracked; the tracked examples (with their questions) are in `domains/examples/`.
- This changed the hash of the question set: on the working database the triage queue holds 9519 repositories (1599 + 7920). The scheduled
  launchd run (every 12 hours) will start re-triage by itself. A facet tag appears for a repository only after
  there is a verdict for it under the new question set. Until then it is shown without tags.
- The schedule was not changed. The digest of 2026-11-10 08:30 has already been sent in the old format.

## Evaluation (by results)

After re-triage finishes, look at the distribution of `p` for each facet question: the share of repositories with `p >= 0.7`.
A question where almost everyone is "yes" or almost everyone is "no" is noise: replace it or disable it (`enabled: false`).
Manual sample: 10 repositories with a high `p` and 10 with a low one, check against the README. The decision to replace weak questions
is taken by the owner.

## Left to the builder

- Code structure, key and command names (except those marked as a proposal), the storage format of the "not shown"
  items.
- Stack changes (`stack_changed`) in the main digest: they are not in the mock-up and were not discussed; by default leave
  them as today, at the end of the section.
- Order inside a section: keep the current one (substance, relevance, stars for new; growth per day for 📈), unless
  a reason to change it is found.
- Pre-release label in releases: show it only if the data is in the payload of the `release` event; otherwise do not show it.
- One release per repository per day: the newest tag is shown.
- Language of captions and headings: English, as now (my decision; translation is possible later).

## Known side observations (out of scope)

- The `kind` field is often set to `framework` for MCP servers that are rather a `tool`.
- In a random sample of accepted `mcp` repositories there are some that do not look like MCP by their description
  (gflow, axonasp, vi-sql): `relevant` false positives are possible. Needs a separate check.
