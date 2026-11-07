#!/usr/bin/env python3
"""P1-T03 spike: run a decider worker on the labelled triage sample, report accuracy and speed.

  uv run python scripts/spike_decider.py                      # d1-3B
  uv run python scripts/spike_decider.py --model d1-2B
  uv run python scripts/spike_decider.py --model d1-3B --keep /tmp/spike   # keep requests.json/out.json

Not a test: it loads a real model (needs ./deciders/setup.sh --download <model> first).
Prints per-item answers, accuracy of `relevant p>=0.7` vs label, mean |substance - label|,
load time and ms/item. Progress goes to stderr, the report to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DECIDERS = ROOT / "deciders"
DOMAIN_PROFILE = (
    "MCP servers & tooling: Model Context Protocol servers, clients, SDKs, inspectors, proxies "
    "and developer tools built around MCP, usable by AI agents or developers."
)
_PLACEHOLDER = re.compile(r"\{(full_name|description|topics|language|stars|is_fork|readme_excerpt|domain_profile)\}")


def render_state(template: str, item: dict) -> str:
    """Fill placeholders in ONE pass so placeholders inside repo text are not expanded again."""
    values = {
        "full_name": item["full_name"],
        "description": item["description"],
        "topics": ", ".join(item["topics"]),
        "language": item["language"],
        "stars": str(item["stars"]),
        "is_fork": str(item["is_fork"]).lower(),
        "readme_excerpt": item["readme_excerpt"].replace('"""', "'''"),
        "domain_profile": DOMAIN_PROFILE,
    }
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def one_line(text: str) -> str:
    return " ".join(str(text).split())


def load_questions() -> tuple[str, dict[str, dict]]:
    cfg = yaml.safe_load((DECIDERS / "questions.yaml").read_text(encoding="utf-8"))
    qs: dict[str, dict] = {}
    for qid, q in cfg["questions"].items():
        if not q.get("enabled", True):
            continue
        d: dict = {"type": q["type"], "instructions": one_line(q["instructions"])}
        crit = q.get("criteria")
        if isinstance(crit, dict):
            d["criteria"] = {k: one_line(v) for k, v in crit.items()}
        elif isinstance(crit, list):
            d["criteria"] = [one_line(v) for v in crit]
        qs[qid] = d
    return cfg["state_template"], qs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="d1-3B", help="name from deciders/models.yaml (default: d1-3B)")
    ap.add_argument("--models-file", type=Path, default=DECIDERS / "models.yaml",
                    help="registry to read (venv/runner may be absolute paths; used for the Clef spike)")
    ap.add_argument("--sample", type=Path, default=ROOT / "tests/fixtures/triage_sample.jsonl")
    ap.add_argument("--keep", type=Path, help="directory to keep requests.json / out.json in")
    ap.add_argument("--timeout", type=int, default=1800, help="worker timeout, seconds")
    args = ap.parse_args()

    models = yaml.safe_load(args.models_file.read_text(encoding="utf-8"))["models"]
    if args.model not in models:
        print(f"error: unknown model {args.model!r}; available: {', '.join(models)}", file=sys.stderr)
        return 2
    m = models[args.model]
    python = DECIDERS / m["venv"] / "bin" / "python"  # absolute venv paths win
    if not python.exists():
        print(f"error: {python} missing; run ./deciders/setup.sh", file=sys.stderr)
        return 2

    worker = Path(m["runner"]) if Path(m["runner"]).is_absolute() else DECIDERS / "workers" / f"{m['runner']}.py"
    items = [json.loads(line) for line in args.sample.read_text(encoding="utf-8").splitlines() if line.strip()]
    template, questions = load_questions()
    requests = []
    rid_map: dict[str, tuple[int, list[str]]] = {}
    for i, it in enumerate(items):
        state = render_state(template, it)
        groups = [list(questions)] if m["grouping"] == "joint" else [[q] for q in questions]
        for qids in groups:
            rid = f"{i}::{'+'.join(qids)}"
            requests.append({"rid": rid, "state": state, "questions": {q: questions[q] for q in qids}})
            rid_map[rid] = (i, qids)

    cfg = {k: m[k] for k in ("repo", "revision", "device", "dtype")}
    env = {k: os.environ[k] for k in ("PATH", "HOME", "HF_HOME", "HF_HUB_CACHE", "TMPDIR") if k in os.environ}
    with tempfile.TemporaryDirectory(prefix="repka-spike-") as tmp:
        work = args.keep or Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        req_path, out_path = work / "requests.json", work / "out.json"
        req_path.write_text(json.dumps(requests, ensure_ascii=False), encoding="utf-8")
        cmd = [str(python), "-I", str(worker),
               "--config", json.dumps(cfg), "--requests", str(req_path), "--out", str(out_path)]
        print(f"running {args.model}: {len(requests)} requests for {len(items)} items", file=sys.stderr)
        t0 = time.perf_counter()
        rc = subprocess.run(cmd, cwd=tmp, env=env, stdout=sys.stderr, timeout=args.timeout).returncode
        wall = time.perf_counter() - t0
        if rc != 0 or not out_path.exists():
            print(f"error: worker exited with {rc}", file=sys.stderr)
            return 1
        out = json.loads(out_path.read_text(encoding="utf-8"))

    answers: dict[int, dict] = {}
    item_ms: dict[int, float] = {}
    errors = 0
    for res in out["results"]:
        i, qids = rid_map[res["rid"]]
        if "error" in res:
            errors += 1
            print(f"  request {res['rid']}: {res['error']}", file=sys.stderr)
            continue
        answers.setdefault(i, {}).update(res["answers"])
        item_ms[i] = item_ms.get(i, 0.0) + res["ms"]

    meta = out["meta"]
    print(f"model {args.model} ({m['repo']}@{m['revision'][:8]}) device={meta['device']} dtype={meta['dtype']}")
    print(f"{'repo':45} {'rel':>5} {'lab':>3} {'sub':>3} {'lab':>3} {'kind':>16} {'ms':>6}")
    correct = abs_err = abs_err_exp = answered = 0
    for i, it in enumerate(items):
        a = answers.get(i)
        if not a or any(q not in a for q in questions):
            print(f"{it['full_name']:45} no answer")
            continue
        answered += 1
        p = float(a["relevant"]["noul"])
        probs = {int(k): float(v) for k, v in a["substance"]["probabilities"].items()}
        pred = max(probs, key=probs.get)
        exp = float(a["substance"]["score"])
        kind = a["kind"]["choice"]
        correct += (p >= 0.7) == it["label_relevant"]
        abs_err += abs(pred - it["label_substance"])
        abs_err_exp += abs(exp - it["label_substance"])
        print(f"{it['full_name'][:45]:45} {p:5.2f} {int(it['label_relevant']):>3} {pred:>3} "
              f"{it['label_substance']:>3} {kind:>16} {item_ms[i]:6.0f}")
    n = answered or 1
    mean_ms = sum(item_ms.values()) / n
    print()
    print(f"answered {answered}/{len(items)} items, request errors {errors}")
    print(f"relevant (p>=0.7) accuracy: {correct}/{answered} = {correct / n:.0%}")
    print(f"substance mean |argmax - label|: {abs_err / n:.2f}   mean |expected - label|: {abs_err_exp / n:.2f}")
    print(f"load {meta['load_s']:.1f} s, {mean_ms:.0f} ms/item (all questions), total wall {wall:.1f} s")
    return 0 if answered == len(items) else 1


if __name__ == "__main__":
    sys.exit(main())
