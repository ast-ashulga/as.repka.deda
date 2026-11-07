"""Fake decider worker following the CLI contract (--config --requests --out); stdlib only.

Behaviour comes from cfg["mode"], or from cfg["repo"] when there is no mode (run_worker builds the config
from a ModelSpec, so tests encode the mode in `repo`):
  ok            answer every request, dump cwd/env/requests dir into meta
  crash_after_1 write out.json with one result, then exit 1
  sleep         sleep and write nothing (for timeout tests)
  bad_answer    answer the first request with a malformed answer, the rest normally
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def answer(questions: dict) -> dict:
    out = {}
    for qid, q in questions.items():
        if q["type"] == "noul":
            out[qid] = {"type": "noul", "noul": 0.9}
        elif q["type"] == "choice":
            keys = list(q["criteria"])
            probs = {k: (0.7 if i == 0 else 0.3 / (len(keys) - 1)) for i, k in enumerate(keys)}
            out[qid] = {"type": "choice", "choice": keys[0], "confidence": 0.7, "probabilities": probs}
        else:
            n = len(q["criteria"])
            probs = {str(i): (0.6 if i == n - 1 else 0.4 / (n - 1)) for i in range(n)}
            out[qid] = {"type": "score", "score": n - 1.2, "confidence": 0.6, "probabilities": probs}
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--requests", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    cfg = json.loads(args.config)
    mode = cfg.get("mode") or cfg["repo"]
    requests = json.loads(args.requests.read_text(encoding="utf-8"))
    meta = {
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "requests_dir": str(args.requests.resolve().parent),
        "config": cfg,
    }

    def save(results: list) -> None:
        args.out.write_text(json.dumps({"meta": meta, "results": results}), encoding="utf-8")

    if mode == "sleep":
        time.sleep(30)
        return
    results = []
    for i, req in enumerate(requests):
        if mode == "bad_answer" and i == 0:
            results.append({"rid": req["rid"], "ms": 1.0, "answers": {}})
            continue
        results.append({"rid": req["rid"], "ms": 1.0, "answers": answer(req["questions"])})
        if mode == "crash_after_1":
            save(results)
            sys.exit(1)
    save(results)


if __name__ == "__main__":
    main()
