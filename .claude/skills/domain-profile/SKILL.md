---
name: domain-profile
description: Use when the user wants to define or refine a GitHub monitoring domain for repka. Interviews the user, researches GitHub with read-only gh calls, then writes and validates domains/<id>.yaml.
---

# Domain profile

Turn the user's idea into one domain file, `domains/<id>.yaml`, that the repka CLI can collect from. You may write only that file, and only under `domains/`. Do not touch other files. The only CLI calls you make are the `domain` inspection commands (`list`, `show`, `plan`), plus read-only `gh api` GETs.

## 1. Interview

Ask one question at a time, and wait for the answer before the next. Cover, in this order:

1. What the domain is, in one sentence.
2. What is in scope and what is out of scope.
3. Three to five known relevant repos ("anchors") as `owner/name`.
4. Two or three known junk examples (repos that match the words but should be rejected).
5. Languages to include (or none).
6. Fork policy: `exclude` (default), `include` or `only`.
7. Scan frequency (default `12h`, minimum `1h`).

If the user gives several answers at once, accept them and only ask for what is still missing.

## 2. Research

For each anchor, harvest topics and vocabulary with a read-only call:

```bash
gh api repos/<owner>/<repo> --jq '.topics,.language,.description'
```

For candidate topic names, search the topic index:

```bash
gh api "search/topics?q=<term>" --jq '.items[].name'
```

Use the harvested vocabulary to propose keyword groups and topics. Show the proposal to the user and let them confirm it.

## 3. Draft

1. Copy `.claude/skills/domain-profile/domain-template.yaml` to `domains/<id>.yaml`. Use a slug id (`[a-z0-9-]`, 2-40 characters).
2. Set `id` to the file stem, and `name` to the human-readable name.
3. Write `profile` for the deciders: inclusions, exclusions, and one example of a match and one of a miss. Keep it plain and at most 1,500 characters.
4. Fill `search`:
   - Each keyword group becomes its own GitHub query, and the words inside a group are ORed. Quote multi-word phrases.
   - `topics` become `topic:<t>` queries.
   - `exclude` words become `NOT <word>`. Keep at most 5 operators per query.
   - Keep `enabled: false`. Set it to `true` only if the user explicitly asks to enable the domain.

## 4. Validate

```bash
uv run repka domain show <id>
uv run repka domain plan <id>
```

If `show` prints an `error:` line, fix the dotted key it names and run it again. `plan` prints the numbered queries and the first-scan window (`pushed` field and start date). For machine-readable input to the probe, use:

```bash
uv run repka domain plan <id> --json
```

## 5. Size probe

Probe each query with the window qualifier appended, so the count matches what the first scan will see. Use `pushed:>=<window start date>` from the plan output. Use a GET request and pass the query with `-f`. Always add `-X GET`, because `gh api` sends POST when `-f` is given without it.

```bash
gh api -X GET search/repositories -f q='<query> pushed:>=<YYYY-MM-DD>' -f per_page=1 --jq .total_count
```

Tune the matrix until:

- each query returns between about 20 and 5,000 results, and
- every anchor is found by at least one query. Check each anchor with `repo:<owner>/<repo>` added to a query, using the same GET form.

Too many results: add a keyword group, a language, `min_stars`, or an `exclude` word. Too few: remove a language or a `min_stars` floor, or broaden a keyword. After each change, run `show` and `plan` again. Keep the number of probes small, and if `gh` reports a rate limit, stop probing and report what you have.

## 6. Finish

Report the file path, the number of queries, the probe counts, and whether every anchor matched. Say that the domain is disabled (`enabled: false`) and that enabling it is a separate, explicit step.

## Rules

- Write only `domains/<id>.yaml`.
- Use only read-only `gh api` GETs. Never use POST, PUT, PATCH or DELETE, and never use `gh` commands that change GitHub (issues, pull requests, repos, labels, comments).
- Never run the pipeline commands (collect, triage, embed, digest, notify, run). They touch GitHub and Telegram and are for the scheduler and the user.
- Leave `enabled: false` unless the user explicitly asks to enable the domain.
