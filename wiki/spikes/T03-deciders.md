# Spike T03: local decider on Apple Silicon

[← Wiki home](../README.md) · Research: [deciders](../research/deciders.md)

Run date: 2026-11-07. Machine: Apple Silicon Mac, MPS available.
Set up the same environments with `./deciders/setup.sh && ./deciders/setup.sh --download d1-3B d1-2B`. The throwaway benchmark script used for these numbers has been removed: it was a standalone driver, not the production triage code path (`repka triage`), so treat the numbers as indicative.

## Setup

- `deciders/` was copied from the reference project (its `classifier/` directory): `models.yaml` revisions are byte-identical, only the header comments, `setup.sh` messages and `README.md` were renamed. Workers are unchanged apart from a docstring pointer.
- `.venv-liquid`: transformers 5.22.2, torch 2.15.0, device `mps`, dtype bfloat16. `.venv-pplx` is created by `setup.sh` too (not exercised here).
- Questions: `deciders/questions.yaml` (`relevant` noul, `substance` score 0..4, `kind` choice, `is_junk` disabled). One state template (see deviation below). Domain profile used: "MCP servers & tooling".
- Sample: 20 real public repos fetched with `gh api` (README excerpt capped at 1,500 chars). 10 "relevant" MCP tools/SDKs (`label_relevant: true`) and 10 negatives: 4 empty/placeholder/artifact repos, 2 SEO-style/demo repos, and big real but off-topic projects (linux, flask, react, gitignore, coding-interview-university). Substance labels (0..4) were assigned by hand and are subjective. The sample file (`tests/fixtures/triage_sample.jsonl`) was removed from the repository later.
- Spike driver: a standalone script (since removed) that built `requests.json` and ran the worker with `-I`, a scratch cwd, a minimal env and offline.

## d1 results (n = 20)

| Model | Revision | Answered | `relevant` p>=0.70 accuracy | substance mean abs error (argmax / expected) | Load | ms/item (all 3 questions, 1 pass) | Wall |
|---|---|---|---|---|---|---|---|
| d1-3B | `051bcc46` | 20/20 | 17/20 = 85% | 0.35 / 0.38 | 12.5 s | 447 | 24.7 s |
| d1-2B | `652f920e` | 20/20 | 20/20 = 100% | 0.50 / 0.35 | 3.1 s | 302 | 11.5 s |

Items near the threshold (relevant p): d1-3B: oraios/serena 0.62 (false negative), ivanlitovsky/mcp_test 0.80 (false positive, empty repo), mulak1/derm-logic 0.80 (false positive, SEO page that mentions "MCP Server"), upstash/context7 0.73 (borderline hit), mcp-fault-localisation-artifacts 0.62 (correct negative). d1-2B: context7 0.71 and derm-logic 0.69 (the 100% rests on a 0.01 margin), mcp_test 0.45, serena 0.84.

Observations:
- Both answered every item with zero request errors; steady-state speed is well under 1 s per repository for all questions, so a 200-repo triage chunk takes ~1.5 min (3B) or ~1 min (2B) plus load.
- The decider reliably separates MCP tooling from big off-topic projects (linux, flask, react: p <= 0.05) and recognises empty placeholders via `substance` 0 (3B: mcp_test, fault-localisation artifacts, derm-logic all `substance` 0; 2B: derm-logic got 3).
- Weak spot: repos that merely mention MCP (empty `mcp_test`, the SEO page). d1-3B gave them `relevant` 0.80 but `substance` 0, so the combined rule below still rejects them; d1-2B gave `mcp_test` 0.45 and derm-logic 0.69 (`substance` 3).
- Combined acceptance with the default `thresholds: {relevant: 0.70, substance: 2}` (accept only if all pass): d1-3B accepts 9 of the 10 intended tools (misses serena, `relevant` 0.62) and 0 negatives (19/20 correct); d1-2B accepts all 10 tools and 0 negatives (20/20), but derm-logic passes `relevant` only by missing 0.70 by 0.01.
- `kind` is sensible but jittery (`playwright-mcp` as `library`/`tool`, `fastmcp` as `framework`); treat it as a label only, as planned.
- Caveat: n = 20 and hand labels; differences of one item (5 points) are noise. This spike shows "works and is fast", not a ranking.

## Verdict: default model

**Default stays `d1-3B`** (as in the code defaults, architecture.md and ADR-003). Honest reading of the numbers: on this sample d1-2B was equal or slightly better (combined rule 20/20 vs 19/20, 302 vs 447 ms/item, 3 s vs 12.5 s load, 4 GB vs 6 GB), while d1-3B had the lower substance error (0.35 vs 0.50). A one-item difference on n = 20 hand-labelled repos is noise, both are fast enough for an hourly batch job, so the original default (the larger model) is kept and nothing downstream changes. **d1-2B is a verified fallback**: set `triage.model: d1-2B` if memory or load time matters. Re-compare both on a larger labelled set once Phase 1 has real data (the verdict table keeps `model` in its key, so switching only re-triages).

## Clef: GO (optional backend)

Verified against the official card ([Cloudflare/clef-flash](https://huggingface.co/Cloudflare/clef-flash), revision `fb4417d31c1d0f9e7e77f1141b40aaede7491e00`, 2026-10-30) and the MLX build that was run.

| Question from [deciders § 2](../research/deciders.md) | Result |
|---|---|
| Official repo id | `Cloudflare/clef-flash` (confirmed, Apache-2.0). Larger sibling: `Cloudflare/clef`. |
| Backbone | Post-trained from `Qwen/Qwen3.5-9B` (with vision encoder) + a "joint schema head" (small transformer over the final hidden states). Confirmed; the card does not call the backbone "frozen", so that part stays unverified. |
| Answer contract | Same as d1/Jev SystemOne: `noul` -> `{noul}`, `choice` -> `{choice, confidence, probabilities}`, `score` -> `{score, confidence, probabilities {"0".."n"}, legend}` (read in `clef_mlx.py::systemone_answer`). The d1 `normalize_answer` works unchanged. |
| OOS weakness | Card table: CLINC150+OOS macro-F1 Clef 97.4 vs Clef-flash 66.8 (so the claim of weaker out-of-scope detection is confirmed). On our sample Clef-flash still rejected all 10 negatives, but with weaker margins (empty `mcp_test` 0.39, artifacts 0.46, derm-logic 0.58). |
| Load via `clef_mlx.py` | Yes (see below). |

Run: `mlx-community/clef-flash-4bit` at revision `b5adbe2c3a95200d4bf04a0727c8ec457157d5e1` (6.2 GB download; 8-bit is `mlx-community/clef-flash-8bit` @ `5a195dc1...`). Throwaway venv (`uv venv --python 3.12`, `uv pip install "mlx-vlm>=0.7.4,<0.8" huggingface_hub pyyaml`, no torch), the unchanged `clef_mlx.py` (reviewed first: only `urllib.request` for image URLs, an optional local HTTP server, no subprocess; the shipped `__pycache__/*.pyc` was NOT imported, the worker copies the `.py` to a temp dir) and a ~25-line worker with the standard worker contract, run through the same driver script.

| Model | Answered | `relevant` p>=0.70 accuracy | substance mean abs error (argmax / expected) | Load | ms/item | Wall |
|---|---|---|---|---|---|---|
| clef-flash-mlx 4-bit (MLX, GPU) | 20/20 | 19/20 = 95% | 0.40 / 0.53 | 5.4 s | 1733 | 46.0 s |

The only miss is `upstash/context7` (0.49, false negative); the run had zero request errors. Combined rule (`relevant>=0.70` and `substance>=2`): 9/10 tools accepted, 0 negatives (19/20, same as d1-3B).

- RAM: the test machine was above the card's 16 GB minimum for the 4-bit build (and the 24 GB suggested for the 8-bit one); peak memory was not measured (the model card quotes ~7 GB peak for 4-bit at 1k tokens).
- Speed: ~4x slower than d1-3B per item (1.7 s vs 0.45 s) and ~6x slower than d1-2B, still fine for a batch of a few hundred repos per run (~6 min per 200 repos).
- Accuracy on this sample: comparable to d1-3B, not better. The case for Clef is a second opinion/comparison, not a quality jump, which fits "optional".
- Needs its own `.venv-mlx` with `mlx-vlm>=0.7.4,<0.8` (tested combination in the card); the main package never imports it.

**Clef: GO.** The backend remains an optional follow-up and is not implemented. It would: add the entry to `models.yaml` (`runner: clef_worker`, `venv: .venv-mlx`, repo `mlx-community/clef-flash-4bit`, revision `b5adbe2c3a95200d4bf04a0727c8ec457157d5e1`, `grouping: joint`), extend `setup.sh` with `.venv-mlx`, write a worker that loads `clef_mlx.py` from the pinned snapshot (copy the `.py` to a temp dir; never import the shipped `.pyc`), and register the model as `clef-flash-mlx`. Pin by full revision; remote code must be re-reviewed before any revision bump.

## Deviations from the original plan

- `deciders/questions.yaml` has one `state_template` (not two). The triage runtime's interface (`QuestionSet(questions, state_template)`) has a single template, and the second template would have been byte-identical.
