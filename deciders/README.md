# deciders — models and questions for `repka triage`

Everything needed for local repository triage with System-1 ("decider") models.
All paths are relative to the project; nothing outside this repo is required.

| File/Dir | Description |
|---|---|
| `questions.yaml` | Triage questions (`relevant`, `substance`, `kind`, `is_junk`) and the state templates. Disable a question with `enabled: false`. |
| `models.yaml` | Model registry: HF repo, pinned revision (a full 40-character commit hash is required), grouping (`joint`/`single`), device, dtype. |
| `workers/` | Model workers (one per family). Each runs in its own venv, reads `requests.json`, writes `out.json`. |
| `setup.sh`, `fetch.py` | Create venvs and download model weights. |
| `.venv-liquid/`, `.venv-pplx/` | Virtual environments created by `setup.sh` (gitignored by `deciders/.venv-*/`). |

## First-time setup

```bash
./deciders/setup.sh                   # create environments
./deciders/setup.sh --download d1-3B  # download weights (~6 GB) to the HF cache
uv run python scripts/spike_decider.py --model d1-3B   # smoke/benchmark on the labelled sample
```

Models run offline by default (HuggingFace cache only). Workers accept `--config` JSON with `"online": true` to allow network access.
Model results of the P1-T03 spike: `wiki/spikes/T03-deciders.md`.
