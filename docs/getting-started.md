# Getting started

[← Back to the README](../README.md)

## Requirements

- macOS on Apple Silicon (the only supported target), [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12).
- A GitHub login: `gh auth login` (repka falls back to `gh auth token`) or a personal access token in `.env`.
  A fine-grained token with read-only public access is enough.
- For triage: about 6 GB of disk for the decider model (`d1-3B`) and its environment.
- Optional: [Ollama](https://ollama.com) with `nomic-embed-text` (embeddings, `repka similar`).
- Optional: the dashboard's dependencies, `uv sync --group dashboard` (plain `uv sync` does not install them).
- Optional: a Telegram account and API credentials from https://my.telegram.org (digest delivery).

## Quick start (humans)

Nothing is sent and nothing is scheduled until you turn it on: every feature flag is `false` by default.

```bash
uv sync
uv run repka doctor                  # lists everything that is missing, each with the exact fix
```

`doctor` reports an error until a domain exists ("no enabled domain"). Fix everything else in its list first, then create
a domain (below) and run `doctor` again until it says all good:

```bash
uv run repka domain new              # 1. describe a domain; writes domains/<id>.yaml (nothing is enabled unless you say y)
uv run repka domain plan <id>        # 2. see the GitHub queries it will run and the first-scan window
```

Turn on triage and embeddings in `.config.yaml` (flags are off by default; see [Configuration](configuration.md)):

```yaml
triage: {enabled: true}   # needs ./deciders/setup.sh and ./deciders/setup.sh --download d1-3B
embed: {enabled: true}    # needs `ollama serve` and `ollama pull nomic-embed-text`
```

```bash
uv run repka doctor                  # again: it now checks the model, its environment and Ollama
uv run repka run                     # first pass: collect -> triage -> embed -> digest PREVIEW (sends nothing)
uv run repka digest                  # preview the pending digest on stdout
```

The first scan of a domain is a silent baseline, so the first preview is empty on purpose: later scans
report what is new since then. Use a short window for a first try (`collect.first_window: 7d`).
When the preview looks right, set up delivery and the schedule:

```bash
uv run repka doctor --send           # what sending needs (Telegram credentials, session, chat)
uv run repka notify login            # one time: phone, code, 2FA
uv run repka notify test "hello"     # needs notify.enabled: true
uv run repka run --send              # deliver the due slot once; run it again: nothing is sent twice
uv run repka schedule install        # hourly launchd agent (set notify.enabled and run.send to true first)
                                     # or fixed daily times: --at 22:00,09:00
```

Optional, once data is flowing: a browser view of the same data, see [Dashboard](dashboard.md).

