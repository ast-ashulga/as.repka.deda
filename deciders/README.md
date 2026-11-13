# deciders — models and questions for `repka triage`

Everything needed for local repository triage with System-1 ("decider") models.
All paths are relative to the project; nothing outside this repo is required except the one-time weight download.

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
./deciders/setup.sh --download d1-3B  # one-time download of the weights (~6 GB) from Hugging Face into the HF cache
uv run repka doctor                   # checks the environment and the weights
uv run repka triage --dry-run         # pending count and the first model input; loads no model
```

Models run offline by default (HuggingFace cache only). Workers accept `--config` JSON with `"online": true` to allow network access.
Measured accuracy and speed of the models on a small labelled sample: [the decider spike report](../wiki/spikes/T03-deciders.md).
