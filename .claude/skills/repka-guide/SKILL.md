---
name: repka-guide
description: User guide and interactive operator for the repka CLI (repka deda, the local GitHub-monitoring tool with Telegram digests). Use whenever the user wants to set up, configure, reconfigure, launch, run, schedule, pause, stop or troubleshoot repka, asks "how do I use repka", "how do I start/stop it", "why is my digest empty", wants to change digest times, domains, Telegram target, triage/embedding settings or the launchd schedule, or asks what state repka is in. Interviews the user about needs and intent, then makes the changes and runs the commands with their approval. Not for defining a new monitoring domain from scratch (use domain-profile for that) and not for developing repka's code.
---

# repka guide

repka watches GitHub for repositories in user-defined domains, filters them with a local model and sends Telegram digests. This skill is the operator's manual: find out what the user wants, check the current state, then walk them through setup, reconfiguration, launching or stopping, doing the actual edits and commands with their consent.

Talk to the user in their language. Keep answers short; show state as a compact table; ask one thing at a time unless several answers are obviously independent.

## Ground rules (they exist because repka sends messages and runs unattended)

1. **Observe before acting.** Always start with the state check in step 1. Advice built on guesses about the current config is how people end up with a silent digest or a surprise message.
2. **Three switches need an explicit "yes" each time**, after you show the exact change: `notify.enabled: true`, `run.send: true`, and `repka schedule install`. Together they make repka message the user's Telegram unattended. Approval for one is not approval for the others, and an earlier "yes" in the conversation does not carry over to a later change.
3. **Never read or print secrets:** `.env`, `.sessions/`. To know whether credentials exist, rely on `repka doctor`, which reports it without exposing values. If the user must put a secret in `.env`, tell them which variable to set and let them edit the file themselves.
4. **Feature flags default to `false`** in this project. When proposing a fresh setup, enable only what the user asked for. When the user turns something off, set it to `false` (do not delete the key).
5. **Do not commit** config changes. `.config.yaml` is tracked, so flag flips show up in `git status`; mention that, and leave committing to the user.
6. **Interactive commands need the user's terminal.** `repka notify login` and `repka domain new` prompt for input. Ask the user to run them with the `!` prefix (for example `! uv run repka notify login`) so the output lands in the session.
7. Run everything from the repo root with `uv run repka ...`. To rehearse without touching the real setup, use a scratch home: `REPKA_HOME=/some/dir uv run repka ...`.

## Step 1: check the current state

Run these (read-only) and summarise them in a table: what is on, what is missing, what is scheduled.

```bash
uv run repka doctor --json          # ok/errors/warnings + the exact fix for each problem
uv run repka domain list            # domains, enabled, scan interval
uv run repka schedule status        # is the launchd agent loaded, last exit code
uv run repka db stats               # row counts and recent runs (0 rows = never run)
```

Also read `.config.yaml` (not `.env`) and note these values: `triage.enabled`, `embed.enabled`, `notify.enabled`, `run.send`, `digest.slots`, `notify.chat_id`, `collect.first_window`. The file is the source of truth for defaults; `references/config-reference.md` explains each key.

If `doctor` is not available (for example `uv` is missing), the user has not completed the install; go to the setup playbook from the top.

## Step 2: find out what the user wants

If their message already says what they want ("stop it", "change digest to 9:00"), skip to the matching playbook. Otherwise ask with `AskUserQuestion` (one question, options below), then follow up with the questions from the chosen playbook.

- **Set up from scratch** → playbook A
- **Change settings** (domains, digest times, Telegram target, triage/embeddings, schedule) → playbook B
- **Run it now** (preview or real send) → playbook C
- **Pause or stop it** → playbook D
- **Something is wrong / empty digest / error** → playbook E, and `references/troubleshooting.md`
- **Just explain how it works** → answer from "How it works" below, no changes

## How it works (for explanations)

`repka run` is one pass: **collect** (GitHub search plus GraphQL snapshots for domains that are due) → **triage** (local decider model decides relevance) → **embed** (local Ollama, powers `repka similar`) → **digest** (built at slots 08:30 and 19:30, sent via Telegram only if sending is on). A failing stage never blocks the digest; the next run continues. Two runs never overlap. The first scan of a domain is a silent baseline: no "new repo" events, so the first digest is empty on purpose. Nothing runs by itself until `repka schedule install`, and that agent runs `repka run` hourly.

## Playbook A: set up from scratch

Walk the list in order; stop and fix whatever `doctor` flags before moving on. Ask only the questions you need.

1. **Intent questions** (ask, with a recommended default in each option):
   - What topic(s) to watch? (Each becomes a domain.) A rough answer is enough; the domain-profile skill refines it.
   - Should it filter with the local model (`triage.enabled`; needs about 6 GB disk and takes ~2 s per repo) or just collect? Without triage nothing is accepted and digests stay empty, so recommend yes unless disk is tight.
   - Want "similar repos" search (`embed.enabled`; needs Ollama)? Optional.
   - Preview only for now, or Telegram delivery? Recommend preview first; delivery is added at step 6 once the preview looks right.
2. **Install and check:** `uv sync`, then `uv run repka doctor`. GitHub auth: `gh auth login` (or the user sets `REPKA_GITHUB_TOKEN` in `.env` themselves).
3. **Define a domain:** hand off to the `domain-profile` skill (it interviews, researches GitHub read-only and writes `domains/<id>.yaml` validated, disabled by default), or the user runs `! uv run repka domain new`. Check with `uv run repka domain plan <id>`. Enable it (`enabled: true` in the domain file) only when the user says so. Tell them to keep `min_stars` and `languages` tight: a broad README keyword can match over a thousand repos per language.
4. **Models (if triage/embed wanted):**
   - Triage: `./deciders/setup.sh` then `./deciders/setup.sh --download d1-3B` (about 6 GB; tell the user before starting the download). Set `triage.enabled: true`.
   - Embeddings: `ollama serve` (user's terminal), `ollama pull nomic-embed-text`, then `embed.enabled: true`.
5. **Rehearse:** suggest a short window for the first try (`collect.first_window: 7d`), then `uv run repka doctor` until it says all good, then `uv run repka run` (collect → triage → embed → digest PREVIEW, nothing is sent). A first scan of ~230 repos took ~8 minutes, so warn the user it can take a while; progress is on stderr and an interrupted run resumes. Remember: the first preview is empty by design; a second pass later shows what is new.
6. **Telegram delivery (only if wanted):**
   - The user puts `REPKA_TG_API_ID` / `REPKA_TG_API_HASH` (from https://my.telegram.org) into `.env` themselves.
   - `! uv run repka notify login` (phone, code, 2FA), once.
   - Ask where digests go: `notify.chat_id` is `"me"` (Saved Messages, the default), a numeric id or `@username`.
   - With an explicit yes: set `notify.enabled: true`, then `uv run repka notify test "hello"`.
   - `uv run repka doctor --send` lists anything still missing.
7. **Scheduling (only if wanted):** explain what it does (hourly `repka run` through a launchd user agent, does not start at login, sends only when `notify.enabled` and `run.send` are both true). With an explicit yes for `run.send: true` and another for the install: `uv run repka schedule install --dry-run` (show the plist, no secrets in it), then `uv run repka schedule install` (`--interval 30m` to change the period), then `uv run repka schedule status`.

End by summarising what is now on, what runs automatically, and how to stop it (playbook D) in two lines.

## Playbook B: reconfigure

First say which keys you are going to change and why, then edit only those keys in `.config.yaml` with the Edit tool (comments are preserved), then run `uv run repka doctor` — config is strict and an unknown key or wrong type is rejected with the dotted key name. Typical requests:

| Want | Change | Note |
|---|---|---|
| Digest at other times | `digest.slots: ["09:00", "18:00"]` (local time) | A slot is sent at most once; a slot older than `digest.max_lateness` is skipped |
| Bigger/smaller digest | `digest.max_items_per_domain` | |
| Fewer false positives / more recall | `triage.thresholds: {relevant: 0.70, substance: 2}` | Higher = stricter. Existing verdicts are kept; only new or changed repos use the new thresholds |
| Different Telegram target | `notify.chat_id` | Needs sending to be on to matter |
| Change run period | `uv run repka schedule install --interval 30m` | Replaces the old agent; needs a "yes" like a fresh install |
| Add a topic | new domain (domain-profile skill) | |
| Tune/disable a domain | edit `domains/<id>.yaml` (`enabled`, `scan_every`, `min_stars`, `languages`) | Use `uv run repka domain plan <id>` to preview queries |
| Move home / rehearse | `REPKA_HOME=<dir>` | After moving `gh`/`uv` or changing `REPKA_HOME`, re-run `schedule install` |

If the change touches `notify.enabled`, `run.send` or the schedule, apply ground rule 2.

## Playbook C: run now

Ask which they want, then run it:

- **Preview** (safe, sends nothing): `uv run repka run`, or `uv run repka run --domain <id>` for one domain (it is collected even when not due; preview only).
- **Cheap look first:** `uv run repka collect --dry-run` (windows and query counts, no network) and `uv run repka triage --dry-run`.
- **Real send, once:** needs `notify.enabled: true`; with an explicit yes, `uv run repka run --send`. Running it again sends nothing twice.
- **Digest only:** `uv run repka digest` previews the pending digest; `--slot HH:MM` previews as that slot.

Read the output to the user: status (ok / partial / failed), counts per stage, and the `hints:` at the end, which explain why a pass did less than expected. `partial` (exit 0) means a stage hit a rate limit or a worker crashed and the next run continues; it is not an emergency.

## Playbook D: pause or stop

Ask what "stop" means to them, because there are levels and the right one depends on the goal. Offer the least destructive one that does the job.

| Goal | Action |
|---|---|
| Stop a pass that is running right now | `pgrep -fl "repka run"` to find it, then `kill <pid>` (SIGTERM). The run lock is released by the OS and the next run resumes; collected data is kept. Confirm with the user before killing. |
| Stop messages but keep collecting | `run.send: false` (and/or `notify.enabled: false`). Events stay pending, so the first digest after resuming may contain old events (known limitation); tell the user |
| Stop the hourly automatic runs | `uv run repka schedule uninstall` (idempotent; unloads the agent and removes the plist). Verify with `uv run repka schedule status`. Config, domains and data are untouched |
| Stop watching one topic | `enabled: false` in `domains/<id>.yaml` |
| Skip the heavy stages | `triage.enabled: false` / `embed.enabled: false` |
| Full stop | uninstall the schedule, set `run.send` and `notify.enabled` to `false`, then confirm with `schedule status` and `pgrep` |

Turning things off needs no extra consent beyond the user's request. To resume later: `uv run repka doctor`, then the reverse change (re-enabling sending or the schedule needs a "yes", ground rule 2).

## Playbook E: troubleshoot

Run `uv run repka doctor` first, then look at the `hints:` of the last `uv run repka run`, then `logs/repka.log` (`logs/launchd.log` for scheduled runs); `uv run repka -v <command>` prints DEBUG on stderr. The common causes (empty digest, rate limits, worker crashes, Ollama down, nothing delivered, "another repka run is in progress") are in `references/troubleshooting.md`. Fix what `doctor` names, one thing at a time, and re-check after each fix.

## Reference files

- `references/config-reference.md`: every `.config.yaml` key, `.env` variables and the domain file fields. Read it when changing a setting you are not sure about.
- `references/troubleshooting.md`: symptom → cause → fix table. Read it for playbook E.
- Project docs: `README.md` (user docs), `wiki/spec/architecture.md` (full config and design).
