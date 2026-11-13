# Run observability: phase, progress and ETA while `repka run` is working

Status: implemented (2026-11-10); see `src/repka/progress/` and `repka status`.
Everything marked **(my call)** was not stated by the owner; check it while reading.

## Why

A pass (`repka run`) can take a long time (a first scan, a triage backlog on a local model). Today stderr
shows a few untimed lines (`collect: 3 enabled domain(s)`, `triage: ai-agent-harness`, ...), the triage worker
prints nothing structured, and nobody can tell **which phase is running, how far it is, or when it will finish**.
Two audiences suffer:

- a person watching the terminal (or tailing the launchd log);
- an agent (or script) that started the run, or arrived later, and must decide "wait, or is it stuck?".

Success: at any moment, from the terminal or from another process, one can answer *what is running, how many
units done of how many, how long it has been going, when it will probably end, and is it still alive*.

## Goals

1. Structured progress events from every stage (collect, triage, embed, digest, releases), with a stage plan up front.
2. Four stderr output modes: `plain` (today, byte-for-byte), `live` (one redrawn status line on a TTY), `lines`
   (timestamped, throttled lines for logs and agents), `jsonl` (one JSON object per stderr line, for programs).
3. An honest ETA: per-stage from measured throughput, overall from throughput plus history, with an explicit
   "lower bound" or "unknown" instead of a made-up number.
4. A machine-readable snapshot, `.state/progress.json`, rewritten atomically while a run holds the lock, and a
   `repka status [--json]` command that tells a live run from a dead one.
5. History for ETAs: per-stage `{seconds, items}` stored in `runs.stats_json["timings"]` (no migration).

## Non-goals and anti-goals

- No dependencies (`rich`, `tqdm`): stdlib only. No web UI, no sockets, no daemon.
- stdout is unchanged in every mode (results and `--json` stay parseable). Progress is stderr only.
- No change to what a pass does, to its order, status, exit codes or digests. Instrumentation must never make a
  stage fail: a reporter error is swallowed and logged at DEBUG.
- Not a promise of accuracy. An ETA that is confidently wrong is worse than none (see "ETA rules").
- No real-time alerts and no network traffic (project constraints 3 and 11).

## Decisions

### D1. Default stays `plain` (my call, owner decides)
Project and global rules: feature flags default to `false`, and existing stderr assertions must hold. So the
selector is `run.progress: plain | live | lines | jsonl` (default `plain`, strict enum, `ConfigError` names
`run.progress`) with `repka run --progress MODE` overriding it. `live` on a non-TTY falls back to `lines`.
**Open question for the owner:** make `live` (TTY) / `lines` (non-TTY) the default? One-line change in `RunConfig`.

### D2. Event model: `Reporter`
`src/repka/progress/` (package):

| module | content |
|---|---|
| `events.py` | `Reporter` protocol, `NullReporter`, `RunProgress` (state machine + snapshot + timings) |
| `eta.py` | `RateEstimator` (EWMA of throughput, injectable clock), `fmt_duration`, history reader |
| `render.py` | `PlainRenderer`, `LinesRenderer`, `LiveRenderer`, `JsonlRenderer`, `make_reporter()` |
| `status.py` | atomic `progress.json` writer, `read_status()`, liveness check |

Reporter calls: `plan(stages)`, `stage_start(stage, steps=None)`, `step(label, unit, total)`,
`advance(done, total=None, detail="")`, `stage_end(status, detail="")`, `note(message)`, `finish(status)`.
A *stage* is collect/triage/embed/digest/releases; a *step* is one unit inside it (a domain, or collect's
"search" and "enrich" phases per domain), with an item count when known.
Everything shown that came from outside (domain ids, repo names) goes through `textutil.printable()`.

### D3. Where events come from
- `run_pass` emits the plan, stage start/end and the per-domain step (it already iterates domains).
- Fine-grained progress is **additive**: `collect_domains(..., reporter=None)`,
  `triage_domain(..., on_progress=None)`, `embed_domain(..., on_progress=None)`. Existing string messages and
  boundary signatures (`Boundaries`) are untouched, so fakes and existing tests keep working.
- Granularity is honest and coarse where the code is coarse: triage advances per chunk of 200 repos (the worker
  subprocess is opaque and answers per chunk); README fetches inside a chunk show only as `detail`. Embed advances
  per batch, collect per query and per enrichment batch. So the triage ETA needs one finished chunk to appear.

### D4. stderr is shared: three writers must cooperate
1. **Logging.** `setup_logging` installs a handler that asks the active reporter to render log records
   (`live`: clear line, print, redraw; `jsonl`: wrap as `{"event":"log",...}`), else writes stderr as today.
2. **Triage worker.** It inherits the stderr fd (`deciders._stderr_fd`). When the active reporter owns stderr
   (`live`/`jsonl`), `run_worker` captures worker output (stdout and stderr merged) and relays it through
   `note`; in `plain` and `lines` nothing changes. Cost: in `live`/`jsonl` worker lines arrive when the
   worker finishes, not as it runs (my call; they are model-loading noise).
3. **CLI prints** during a run (header, lock busy, crash message, final summary line under `--json`) go through
   `progress.stderr_line()`, so in `jsonl` they are JSON too and in `live` they do not corrupt the status line.

In `jsonl` mode every stderr line of `repka run` is valid JSON, including preflight findings, the lock-busy message
and a `ConfigError` raised after the mode is known (an error in loading `.config.yaml` itself happens before the
mode exists and stays a plain `error:` line). Collect's per-query and per-batch note lines are not forwarded as
notes (the progress events carry them, throttled).

### D5. ETA rules
- In-step ETA appears only after `done > 0` and at least 3 s elapsed; rate is an EWMA (alpha 0.3) of recent
  throughput seeded with the cumulative rate.
- Stage ETA = in-step remaining / rate + remaining steps x mean finished-step duration (when `steps` known).
- Overall ETA = current stage ETA + for each later stage the median `seconds` of that stage over the last 5
  runs that did work in it (`items > 0`; a stage that did nothing, such as a not-due collect, is not a sample).
  Older runs without `timings` are ignored.
- If any remaining part has no estimate the overall ETA is a **lower bound** (`eta >2m`); with nothing known it
  is `eta ?`. A finished-at wall-clock time is shown next to a known ETA. Elapsed is always shown.
- A stage that overruns its history (`elapsed > median`) or whose finished steps were all shorter than 3 s (an empty
  first domain) gives a lower bound, never an exact small number. A throughput sample is the interval between two
  updates where `done` grew, so README fetching before a chunk counts toward that chunk's rate.
- Rate-limit waits and worker model loads are not predictable: the ETA is "at the current pace", said so in docs.

### D6. Output formats
`lines`: `[14:03:11] 3/5 triage ai-agent-harness (2/5) 400/1200 repos 33% | 1m20s | eta ~2m40s (at 14:05)`;
emitted on every stage/step change and at most every 10 s for advances; one line at the end.
`live` (TTY, redraw at most 5x/s plus a 1 s ticker thread so elapsed keeps moving): finished stages are printed as
permanent lines (`ok collect 2m10s`), the current one is a single redrawn line with a bar, trimmed to the terminal
width; ASCII bar if stderr is not UTF-8. `jsonl`: events `plan`, `stage_start`, `step`, `progress` (throttled to 1/s),
`stage_end`, `log`, `note`, `summary`, each with `ts`, plus the snapshot fields of D7.
On Ctrl-C or a crash the live line is ended with a newline and the status file is finalised as `failed`
(hooked into the existing crash net in `run_cli._run_locked`).

### D7. Status file and `repka status`
`.state/progress.json`, written atomically (temp file + `os.replace`) **whenever a run holds the lock, in every
mode** (it is bookkeeping like the lock; it is not gated by `run.progress`, so a launchd run on default config
is still observable). Fields: `schema` (1), `pid`, `run_id`, `status` (`running|ok|partial|failed`), `mode`,
`started_at`, `updated_at` (heartbeat, refreshed at least every 5 s while work advances), `stages[]` (name,
status, seconds, items), `current` {stage, step, unit, done, total, detail, rate, eta_seconds}, `overall`
{eta_seconds, eta_is_lower_bound, eta_at}.
A daemon thread refreshes `updated_epoch` (the heartbeat: the process is alive) every 5 s even when no event arrives
(one long worker call); `progress_epoch` moves only with real events (work is advancing), so a quiet run is told
from a frozen process and a hung worker is not hidden behind a fresh heartbeat. `repka status [--json]`: the lock is the truth. It tries a non-blocking flock on `run.lock`: acquired means no run
is alive (file with `status: running` is reported as "not running; last run died at ..."), busy means a live run
(snapshot, heartbeat age and last-progress age; a warning "heartbeat lost" past 120 s, and "no progress ... may be
stuck" past 15 min, generous because one triage chunk of 200 repos takes about 7 min) **when the snapshot says `running` and its pid
exists**; a held lock without that (`repka collect`, `triage`, `digest --send` take the same lock) is `busy`. Exit 0 whenever the state can be
reported; 2 only on usage/config errors.

## Constraints carried from the project
All flags false by default (D1, no other flag added); config loader strict; stdout unchanged; tests never touch
network and use an injected clock; `printable()` on outside text; docs updated in `architecture.md` (sections 3.2, 5 and 7)
and README; unknown or old rows tolerated (`timings` absent).

## Left to the builder
Exact glyphs and widths, the bar style, field order in JSON lines, how the ticker thread is stopped, internal
naming. Tuning constants (3 s, 0.3, 10 s, 120 s, 5 runs) live in one place and may be adjusted if a rehearsal shows
they are wrong.

## Calls I made (check these)
D1 default `plain`; D4.2 worker output relayed only after the worker ends in `live`/`jsonl`; D5 constants and the
"median of last 5 working runs" history rule; status file always written under the lock; `timings` stored in
`runs.stats_json` but not added to the stdout `--json` payload (keeps stdout byte-stable); no `--progress-file`.
