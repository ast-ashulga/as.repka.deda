<div align="center">

# repka deda

**A private GitHub radar. Tell it what you care about; it reads GitHub for you and sends a Telegram digest of what is new.**

Local models decide what is relevant. Your repository data never leaves your Mac except as GitHub API calls and the digest itself; the only other traffic is one-time model downloads.

[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://docs.astral.sh/uv/)
[![macOS Apple Silicon](https://img.shields.io/badge/macOS-Apple%20Silicon-000000?style=flat-square&logo=apple&logoColor=white)](docs/getting-started.md#requirements)
[![Local first](https://img.shields.io/badge/local--first-no%20hosted%20models-2ea44f?style=flat-square)](#why-repka)
[![Every flag off by default](https://img.shields.io/badge/feature%20flags-off%20by%20default-lightgrey?style=flat-square)](docs/configuration.md)
[![Agent friendly](https://img.shields.io/badge/CLI-JSON%20output%20for%20agents-8A2BE2?style=flat-square)](#using-it-with-an-ai-agent)

[Sample digest](#what-lands-in-your-telegram) ·
[Use it with an AI agent](#using-it-with-an-ai-agent) ·
[Try it](#try-it-in-5-minutes) ·
[User guides](#user-guides) ·
[Troubleshooting](docs/troubleshooting.md)

</div>

---

## ❌ Without repka

- You skim trending pages, star lists and newsletters, and still miss the repo that mattered.
- A GitHub search for "mcp server" returns over a thousand repositories per language. Most are noise.
- Releases, star spikes and dependency changes of the projects you follow are scattered over a dozen places.

## ✅ With repka

You describe a technology area once (a **domain**, for example "AI agent harnesses" or "MCP servers"). Twice a day
repka finds new and fast-growing repositories in it, lets a local decider model drop the irrelevant ones, and sends
you one short digest: 🆕 new repos, 📈 star spikes, 🧩 stack changes and 🚀 releases.

## What lands in your Telegram

repka sends one short message per slot (08:30 and 19:30 by default; the header says *morning* before noon and
*evening* after). It is grouped by **domain**, a technology area you defined, and lists only what changed since the
last digest. An empty digest is never sent.

This is the *insights* format (`digest.insights: true`; optional, off by default), adapted from a real run and
shortened. Releases go in their own daily message (`digest.releases_message`), shown second:

```text
repka · morning digest · 2026-11-13

AI Agent Harness
🆕 ArtlexYoung/dsh-super-code ★35
   A faster, more economical, more accurate DeepSeek harness plugin for coding…
   library · lvl 4
📈 ruvnet/ruflo ★81.6k (+74★/24h)
   The original agent harness. Deploy intelligent multi-player swarms…
   framework · lvl 4 · subagents
📈 first-fluke/oh-my-agent ★1.4k (+64★/24h)
   Agent skills and specialist teams for Claude Code, Codex, Cursor…
   framework · lvl 4 · subagents · sandbox
🧩 k4yt3x/meka Cargo.toml +os_info

🆕 new repo · 📈 star spike · 🧩 stack change · tags = decider answers (p ≥ 0.70)
…and 5 more: 5 🧩 (repka digest --more)
```

```text
repka · releases · 2026-11-13

AI Agent Harness · 62 releases
🚀 ruvnet/ruflo ★81.6k · v3.77.0
🚀 langchain-ai/deepagents ★31.3k · deepagents==0.7.37
…and 60 more
Full list: repka releases --since 24h
```

### How to read an item

Every item has up to three lines. Here is one, annotated (the comments are not part of the message):

```text
📈 first-fluke/oh-my-agent ★1.4k (+64★/24h)    # what happened · repo · stars (and stars gained in the last 24 h)
   Agent skills and specialist teams for…       # the repo's GitHub description, cut at 140 characters
   framework · lvl 4 · subagents · sandbox      # kind · maturity level · domain tags
```

**The icon says what happened:**

| Icon | Meaning | When it appears |
|---|---|---|
| 🆕 | New repo | First seen in this domain, and created within the last 90 days |
| 🔎 | Found | First seen in this domain, but older than 90 days (it just entered your radar) |
| 📈 | Star spike | Gained at least 50 stars in about 24 hours |
| 🧩 | Stack change | A dependency was added or removed in a tracked manifest (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`); shown as `Cargo.toml +os_info −old_dep` |
| 🚀 | Release | A new stable release (pre-releases are skipped); in the main digest, or in the releases message |

The thresholds above are the defaults. The first scan of a domain is a silent baseline, so nothing is reported as new
until a later scan.

**The third line is what the local decider model concluded:**

- **kind** is what sort of project it is: `tool`, `library`, `framework`, `protocol_spec`, `app_service`,
  `model_or_dataset`, `awesome_list`, `tutorial_course`, `demo_example` or `other`. It is a label only.
- **lvl N** is how substantial the repo looks, on a scale from 0 to 4: **0** empty or name-squat, **1** toy or
  demo, **2** early but real code, **3** a working tool or library others can use, **4** mature, documented and
  maintained. By default a repo reaches the digest only if it is judged relevant to the domain (probability ≥ 0.70)
  and is at least `lvl 2`; both limits are in `triage.thresholds`.
- **tags** (`subagents`, `sandbox`, ...) are answers to yes/no questions that the domain file itself defines under
  `triage.questions`, so they differ from domain to domain (an MCP domain might ask `remote transport`, `gateway` or
  `guardrails`). A tag is shown when the model's confidence is at least 0.70 (`digest.facet_min_p`), strongest first,
  at most three. Tags never decide whether a repo is accepted; they tell you how an accepted repo differs from its
  neighbours.

**Order and limits.** Within a domain: new and found repos first (higher `lvl`, then relevance, then stars), then star
spikes (biggest gain first), then stack changes, then releases. At most 10 items per domain are shown
(`digest.max_items_per_domain`); the rest are counted in the footer and wait up to 7 days for `repka digest --more`.
The footer line is a legend for the icons that appear in that digest.

## Using it with an AI agent

repka is built to be set up and operated by a coding agent as well as by a person. Every command an agent needs is
non-interactive (except `domain new` and `notify login`), stdout carries results, stderr carries progress and
`error: <message>`, and the exit code says what to do next: `0` ok, `1` runtime failure, `2` fixable usage or
configuration error.

**Paste this into Claude Code (or any coding agent) from the repo root:**

```text
Set up repka from https://github.com/ast-ashulga/as.repka.deda (clone it, then read CLAUDE.md and
docs/agents.md). Run `uv sync`, then `uv run repka doctor --json` and apply the `fix` of every error.
Use the domain-profile skill (.claude/skills/domain-profile/SKILL.md) to write one domains/<id>.yaml
with me, then run `uv run repka run --json` and show me the digest preview. Never enable
notify.enabled, run.send or `repka schedule install` yourself, and never read .env or .sessions/.
```

| An agent wants to | It runs | It gets |
|---|---|---|
| Learn what is missing | `uv run repka doctor --json` | `{ok, errors, warnings, checks[{id, status, title, fix}]}`; it runs the `fix` of each error |
| Define a domain | the `domain-profile` skill, or `repka domain new --from-file brief.txt` | A validated `domains/<id>.yaml` (the skill may write only that file) |
| Check a domain before scanning | `repka domain plan <id> --json` | The GitHub queries and the first-scan window |
| Run a pass | `uv run repka run --json` | Stages, errors, hints and the digest text |
| See what is waiting | `uv run repka digest --more`, `repka releases` | Items queued for a later digest, release events (read-only) |
| Rehearse safely | `REPKA_HOME=/some/dir uv run repka ...` | A scratch home with its own config, `.env`, `domains/` and state |

Hard limits for agents: they never turn on `notify.enabled`, `run.send` or `repka schedule install` on a person's
behalf (a human decides those switches), never read `.env` or `.sessions/` (they hold secrets), and do not launch the
dashboard unless asked (it is a long-running server). The full agent guide is **[docs/agents.md](docs/agents.md)**;
developer conventions are in [CLAUDE.md](CLAUDE.md).

## Why repka

- **Local first.** Triage runs on a local model, embeddings on local Ollama. Only GitHub API calls and the Telegram
  digest leave the machine (model weights are downloaded once from Hugging Face, and `ollama pull` fetches the
  embedding model).
- **Safe by default.** Every feature flag is `false`. Nothing is sent and nothing is scheduled until you turn it on;
  `repka run` prints a preview instead.
- **It does not get in your way.** A failing stage never blocks the digest, a rate limit makes a run `partial` and the
  next run continues, and an empty digest is never sent.
- **You can see what it is doing.** `repka doctor` lists everything that is missing with the exact fix;
  `repka status` shows stage, progress and ETA of a running pass.
- **Agent friendly.** `doctor --json` and `run --json` are stable interfaces, and a Claude skill writes domain files
  for you (see [above](#using-it-with-an-ai-agent)).

```mermaid
flowchart LR
    GH[(GitHub<br/>search + GraphQL)] --> C[collect]
    C --> T[triage<br/>local decider]
    T --> E[embed<br/>local Ollama]
    E --> D[digest]
    D --> TG[/Telegram/]
    DB[(SQLite<br/>.state/repka.db)] --- C
    DB --- T
    DB --- D
```

## Try it in 5 minutes

Preview only: nothing is sent and nothing is scheduled.

```bash
git clone https://github.com/ast-ashulga/as.repka.deda.git && cd as.repka.deda
uv sync                      # installs everything, fetches Python 3.12
uv run repka doctor          # lists what is missing, each item with its exact fix
uv run repka domain new      # describe what to watch; writes domains/<id>.yaml (or copy one from domains/examples/)
uv run repka run             # one pass; prints the digest PREVIEW
```

The first scan of a domain is a silent baseline, so the first preview is empty on purpose; later scans report what
is new. Triage is off by default, so until you turn it on nothing is accepted and the digest stays empty: run
`./deciders/setup.sh` and `./deciders/setup.sh --download d1-3B` (a one-time download of about 6 GB from Hugging Face),
then set `triage.enabled: true` in `.config.yaml`. `doctor` names each missing step. Delivery to Telegram and
scheduling come next in **[Getting started](docs/getting-started.md)**.

## User guides

| Guide | What is in it |
|---|---|
| [Getting started](docs/getting-started.md) | Requirements, the full quick start, delivery and schedule |
| [Domains](docs/domains.md) | The domain file, `min_stars`, facet questions |
| [Configuration](docs/configuration.md) | The `.config.yaml` switches that matter, secrets |
| [Running repka](docs/running.md) | collect, triage, embeddings, `doctor`, `run`, watching a run (`repka status`) |
| [Telegram delivery](docs/telegram.md) | Login, slots, the insights digest, the releases message |
| [Scheduling](docs/scheduling.md) | The launchd agent: hourly or at fixed times |
| [Keeping the database small](docs/database.md) | `repka db prune` and what it removes |
| [Dashboard](docs/dashboard.md) | The local Streamlit view (reads the database; two features write) |
| [Using repka with AI agents](docs/agents.md) | Non-interactive interface and safety rules |
| [Troubleshooting](docs/troubleshooting.md) | Symptoms, fixes, known limitations |
| [Architecture](wiki/spec/architecture.md) | The full config key list and design; plan and tasks in [wiki/](wiki/README.md) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to propose a change; developer and agent notes are in
[CLAUDE.md](CLAUDE.md). `uv run pytest -q` (never touches the network) and `uv run ruff check src tests` must pass.
