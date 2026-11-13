# Research: Decider models for triage

[← Wiki home](../README.md) · Related: [ADR-003/004](../spec/decisions.md)

Note: "the reference project" in these notes is an earlier private prototype that ran the same decider kit. It is not published.

Status: researched 2026-11-07. Secondary sources about Jev/Clef disagree with each other on details. Everything marked **(unverified)** must be confirmed against the official model card (see the [decider spike](../spikes/T03-deciders.md)).

## 1. What a "decider" is

A decider (System-1 / decision model) does not generate text. Given a `state` (the input text) and a set of typed questions, it returns calibrated numbers:

| Question type | Returns | Can be a threshold? |
|---|---|---|
| `noul` | probability `p` that the answer is "yes" (0..1) | yes (`p >= x`) |
| `choice` | one option from a fixed set + `confidence` + per-option `probabilities` | no (shown as a label) |
| `score` | expected level on an ordered scale (`score`), per-level `probabilities`, `confidence`; `pred` = argmax level | yes (`pred >= level`) |

This is exactly the triage the requirements ask for: "Is it relevant to our domain? Is it a ready tool/protocol or junk/placeholder?" — binary (`noul`) + quick classification (`choice`/`score`). It is cheap, deterministic enough, and never hallucinates prose into the digest.

## 2. Candidate models

| Model | Kind | Local on Apple Silicon? | Status for this project |
|---|---|---|---|
| **LiquidAI d1-3B / d1-2B** | open weights, HF, `trust_remote_code`, transformers ≥ 5.19, runs on MPS (bf16) | **Yes — proven** in the reference project, revisions pinned | **Default backend.** ~6 GB / ~4 GB. `grouping: joint` (all questions in one pass). |
| **perplexity pplx-decider-flash v1.2** | open weights, HF, own transformers version | **Yes — proven** in the reference project | Second backend. ~9 GB bf16 (fp32 ~18 GB). `grouping: single`. |
| **Cloudflare Clef / Clef-flash** | open weights (Apache-2.0 per secondary sources); frozen Qwen backbone + ~256 MB trained head **(unverified)**; Clef-flash ≈ 9B **(unverified)** | Community MLX conversions exist (`mlx-community/clef-flash-8bit`, `aufklarer/Clef-flash-9B-MLX-4bit`, …), loaded via a bundled `clef_mlx.py`, not a chat model. 4-bit ≈ 6.2 GB, needs ≥ 16 GB RAM; 8-bit ≈ 10.7 GB, 24 GB recommended **(unverified)** | **Optional backend**, gated by a spike. Official repo id `Cloudflare/clef-flash` confirmed by [spike T03](../spikes/T03-deciders.md#clef-go-optional-backend) (Apache-2.0, Qwen3.5-9B base + joint schema head; MLX build runs, verdict GO). One source reports Clef-flash is much weaker on out-of-scope detection (CLINC150 OOS 66.8 vs 97.4 for Clef) — relevant for "is this junk?" questions. |
| **TypeSafe Jev** | hosted API only, waitlist; same three question types | **No** (weights not public) | Conflicts with "local data". Treated as an **optional remote backend, off by default** (`triage.allow_remote: false`). Endpoint unverified: sources list `api.typesafe.ai/v1/systemone`, OpenRouter, AI/ML API. Not part of the Phase 1 critical path. |

"jev-like" in the requirements is read as *a local decision model of the Jev kind* (typed noul/choice/score answers), not a dependency on the hosted Jev API. See [ADR-004](../spec/decisions.md#adr-004-jev-like-means-a-local-decider-hosted-jev-is-optional-and-off).

## 3. Integration pattern (reused from the reference project)

The reference project already solves the hard parts; this project copies the pattern and, where possible, the files:

- `deciders/` directory (reference: `classifier/`) is the single source of truth: `models.yaml` (registry, **full 40-char pinned revisions**), `questions.yaml`, `workers/*.py`, `setup.sh`, `fetch.py`.
- Each model family has **its own venv** (`deciders/.venv-liquid`, `.venv-pplx`, `.venv-mlx`) because their `transformers`/`mlx` versions conflict. The main package never imports torch/transformers/mlx.
- The main process writes `requests.json`, runs `deciders/<venv>/bin/python -I workers/<runner>.py --config <json> --requests <file> --out <file>` as a subprocess with a **scratch cwd** and a **minimal env with no secrets**, then reads `out.json`.
- Worker I/O contract (keep identical so the workers are drop-in):
  - `requests.json`: `[{"rid": str, "state": str, "questions": {qid: {type, instructions, criteria}}}]`
  - `out.json`: `{"meta": {...}, "results": [{"rid", "ms", "input_tokens", "answers": {qid: {...}}} | {"rid", "error"}]}`
  - raw answers: `noul → {"noul": p}`, `choice → {"choice", "confidence", "probabilities"}`, `score → {"score", "confidence", "probabilities": {level: p}}`.
  - Workers save `out.json` every 10 requests, so a killed worker's partial results are used.
- Workers run **offline** (`HF_HUB_OFFLINE=1`) unless `--online` (default off).
- Model code is remote code: never unpin revisions without reviewing the code (`setup.sh --code-only <name>`).

## 4. Triage questions for repositories (default set)

State text (built by `triage.render_state`), all repo text is untrusted and goes in as data:

```
GitHub repository: {full_name}
Description: {description}
Topics: {topics}   Language: {language}   Stars: {stars}   Fork: {is_fork}
README (excerpt):
"""
{readme_excerpt}
"""

Domain of interest:
"""
{domain_profile}
"""
```

| id | type | question (instructions) | threshold |
|---|---|---|---|
| `relevant` | noul | Is this repository directly relevant to the domain of interest described above? | `p >= 0.70` |
| `substance` | score | How substantial and usable is this repository? levels: 0 empty/placeholder/name-squat · 1 toy/demo/homework · 2 early but real code · 3 working tool/library others can use · 4 mature, documented, maintained | `pred >= 2` |
| `kind` | choice | What is it? `tool` · `library` · `framework` · `protocol_spec` · `app_service` · `model_or_dataset` · `awesome_list` · `tutorial_course` · `demo_example` · `other` | label only |
| `is_junk` | noul (disabled by default) | Is it spam, an SEO/link farm, a crypto pump, or an empty template? | — |

Domains may add questions or override instructions in their domain file (`triage.questions`). A repo is **accepted** when it passes every threshold.

## 5. Performance expectations

- Model load ≈ a few seconds per run (reference project). Batch all repos of a run into one worker invocation.
- Re-triage only when the triage input changes: `input_hash = sha256(state)`; the verdict is keyed by `(repo_id, domain_id, model, questions_hash)`.

## Sources

- [Cloudflare blog: Introducing Clef](https://blog.cloudflare.com/clef-decision-models/)
- [Cloudflare changelog: Clef on Workers AI](https://developers.cloudflare.com/changelog/post/2026-10-01-clef-workers-ai/)
- [eesel.ai: What is Cloudflare Clef](https://www.eesel.ai/blog/cloudflare-clef) (secondary)
- [Towards AI: Clef vs Jev](https://pub.towardsai.net/what-is-clef-the-cloudflare-model-that-competes-with-jev-076995ab2bc7) (secondary)
- MLX conversions: [mlx-community/clef-flash-8bit](https://huggingface.co/mlx-community/clef-flash-8bit), [aufklarer/Clef-flash-9B-MLX-4bit](https://huggingface.co/aufklarer/Clef-flash-9B-MLX-4bit), [FluidInference/clef-flash-coreml](https://huggingface.co/FluidInference/clef-flash-coreml)
- Jev: [OpenRouter: What is Jev](https://openrouter.ai/blog/insights/what-is-jev/), [AI/ML API docs](https://docs.aimlapi.com/api-references/decision-models/typesafe/jev), [Braintrust: what is Jev](https://www.braintrust.dev/articles/what-is-jev) (secondary; endpoints disagree)
- Reference implementation: an earlier private prototype (not published)
