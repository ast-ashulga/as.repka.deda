"""Decider runtime: questions + model registry from deciders/, worker subprocesses, normalized answers.

deciders/ (questions.yaml + models.yaml + workers/) is the single source of truth. Models run in their own
virtualenvs through workers that take a requests JSON and write an answers JSON; nothing heavy (torch,
transformers, mlx) is ever imported here.

Model output is probabilities, not text: every enabled question gets an answer (`noul` -> P(yes),
`choice` -> key, `score` -> level).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from repka import config
from repka.config import ConfigError
from repka.models import TriageItem
from repka.progress.active import owns_stderr, stderr_line

QTYPES = ("noul", "choice", "score")
GROUPINGS = ("joint", "single")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


class WorkerError(RuntimeError):
    """The model worker crashed, timed out or produced nothing (a runtime failure, not a bad config)."""


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def _read_yaml(path: Path, what: str) -> object:
    if not path.exists():
        raise ConfigError(f"{what} not found: {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc


# --- questions ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionSet:
    questions: dict[str, dict[str, Any]]  # enabled questions in file order
    state_template: str


def _parse_question(qid: str, raw: Any, where: str) -> dict[str, Any] | None:
    """One question as {type, instructions, criteria}; None when disabled."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: question {qid!r} must be a mapping")
    if "enabled" in raw and not isinstance(raw["enabled"], bool):  # the string "false" is truthy
        raise ConfigError(f"{where}: question {qid!r}: enabled must be true or false (got {raw['enabled']!r}).")
    if not raw.get("enabled", True):
        return None
    qtype = raw.get("type")
    if qtype not in QTYPES:
        raise ConfigError(f"{where}: question {qid!r}: type must be one of {list(QTYPES)}, got {qtype!r}")
    crit = raw.get("criteria")
    if qtype == "choice" and not (isinstance(crit, dict) and len(crit) >= 2):
        raise ConfigError(f"{where}: question {qid!r}: choice needs `criteria` mapping with 2+ options")
    if qtype == "score" and not (isinstance(crit, list) and 2 <= len(crit) <= 10):
        raise ConfigError(f"{where}: question {qid!r}: score needs `criteria` list of 2-10 levels")
    criteria: dict[str, str] | list[str] | None = None
    if qtype == "choice":
        criteria = {str(k): _one_line(v) for k, v in crit.items()}
    elif qtype == "score":
        criteria = [_one_line(v) for v in crit]
    return {"type": qtype, "instructions": _one_line(raw.get("instructions", "")) or None, "criteria": criteria}


def load_questions(path: Path) -> QuestionSet:
    """Enabled questions of deciders/questions.yaml, file order kept."""
    cfg = _read_yaml(path, "Questions file") or {}
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path.name}: expected a mapping.")
    raw_questions = cfg.get("questions") or {}
    if not isinstance(raw_questions, dict):
        raise ConfigError(f"{path.name}: 'questions' must be a mapping of id -> question.")
    questions: dict[str, dict[str, Any]] = {}
    for qid, raw in raw_questions.items():
        parsed = _parse_question(str(qid), raw, path.name)
        if parsed is not None:
            questions[str(qid)] = parsed
    if not questions:
        raise ConfigError(f"{path.name}: no enabled questions.")
    template = cfg.get("state_template")
    if not isinstance(template, str) or not template.strip():
        raise ConfigError(f"{path.name}: state_template is missing.")
    return QuestionSet(questions=questions, state_template=template)


def merge_questions(base: QuestionSet, overrides: dict) -> QuestionSet:
    """Domain `triage.questions`: same id replaces (keeps its position), new id appends, enabled: false removes."""
    questions = dict(base.questions)
    for qid, raw in (overrides or {}).items():
        parsed = _parse_question(str(qid), raw, "triage.questions")
        if parsed is None:
            questions.pop(str(qid), None)
        else:
            questions[str(qid)] = parsed
    if not questions:
        raise ConfigError("triage.questions: no enabled questions left after merging.")
    return QuestionSet(questions=questions, state_template=base.state_template)


def questions_hash(q: QuestionSet) -> str:
    """Identity of the question set (verdicts are keyed by it): sha256 of canonical JSON, first 16 hex."""
    canonical = json.dumps(
        {"questions": q.questions, "state_template": q.state_template},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# --- models ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    name: str
    runner: str
    venv: str
    repo: str
    revision: str
    grouping: Literal["joint", "single"]
    device: str
    dtype: str
    remote: bool = False


def load_models(path: Path) -> dict[str, ModelSpec]:
    data = _read_yaml(path, "Model registry")
    raw = data.get("models") if isinstance(data, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"{path.name}: 'models' must be a non-empty mapping.")
    models: dict[str, ModelSpec] = {}
    for name, m in raw.items():
        if not isinstance(m, dict):
            raise ConfigError(f"{path.name}: model {name!r} must be a mapping")
        missing = [k for k in ("runner", "venv", "repo") if not m.get(k)]
        if missing:
            raise ConfigError(f"{path.name}: model {name!r} is missing {', '.join(missing)}")
        revision = str(m.get("revision") or "")
        if not _COMMIT_SHA.fullmatch(revision):
            raise ConfigError(
                f"{path.name}: model {name!r}: revision must be a full 40-character commit hash "
                "(the model repository's code runs on this machine, so it is never loaded from a moving branch)."
            )
        grouping = str(m.get("grouping", "joint"))
        if grouping not in GROUPINGS:
            raise ConfigError(f"{path.name}: model {name!r}: grouping must be one of {list(GROUPINGS)}")
        remote = m.get("remote", False)
        if not isinstance(remote, bool):
            raise ConfigError(f"{path.name}: model {name!r}: remote must be true or false (got {remote!r}).")
        models[str(name)] = ModelSpec(
            name=str(name),
            runner=str(m["runner"]),
            venv=str(m["venv"]),
            repo=str(m["repo"]),
            revision=revision,
            grouping=grouping,  # type: ignore[arg-type]
            device=str(m.get("device", "auto")),
            dtype=str(m.get("dtype", "auto")),
            remote=remote,
        )
    return models


# --- requests for the workers ---------------------------------------------------------------------


def request_id(item: TriageItem) -> str:
    """The worker-protocol id of an item: domain and repo."""
    return f"{item.domain_id}:{item.repo_id}"


def _payload(q: QuestionSet, qids: Sequence[str]) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for qid in qids:
        question = q.questions[qid]
        d: dict[str, Any] = {"type": question["type"]}
        if question["instructions"]:
            d["instructions"] = question["instructions"]
        if question["criteria"] is not None:
            d["criteria"] = question["criteria"]
        payload[qid] = d
    return payload


def build_requests(items: Sequence[TriageItem], q: QuestionSet, grouping: str) -> list[dict]:
    """Worker requests. joint: one per item with all questions; single: one per (item, question)."""
    requests: list[dict] = []
    for item in items:
        if grouping == "joint":
            requests.append({"rid": request_id(item), "state": item.state, "questions": _payload(q, list(q.questions))})
        else:
            for qid in q.questions:
                requests.append(
                    {"rid": f"{request_id(item)}#{qid}", "state": item.state, "questions": _payload(q, [qid])}
                )
    return requests


# --- running a worker -----------------------------------------------------------------------------


def _stderr_fd() -> Any:
    """Worker progress goes to stderr so stdout stays clean for --json."""
    try:
        return sys.stderr.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return subprocess.DEVNULL


def _relay(output: str | bytes | None) -> None:
    """Pass captured worker output on to whoever owns stderr, line by line."""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    for line in (output or "").splitlines():
        if line.strip():
            stderr_line(line)


def _read_out(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_worker(
    spec: ModelSpec, requests: list[dict], *, deciders_dir: Path, timeout_s: int, online: bool
) -> dict:
    """Run the worker for `spec` in its own venv; returns the worker's out.json."""
    venv_py = deciders_dir / spec.venv / "bin" / "python"
    worker = deciders_dir / "workers" / f"{spec.runner}.py"
    if not venv_py.exists():
        raise ConfigError(f"No environment {venv_py}; run ./setup.sh in {deciders_dir}.")
    if not worker.exists():
        raise ConfigError(f"Worker {worker} not found.")
    wcfg = {
        "repo": spec.repo,
        "revision": spec.revision,
        "device": spec.device,
        "dtype": spec.dtype,
        "online": online,
    }
    with tempfile.TemporaryDirectory(prefix="repka-triage-") as tmp:
        req_path, out_path = Path(tmp) / "requests.json", Path(tmp) / "out.json"
        req_path.write_text(json.dumps(requests, ensure_ascii=False), encoding="utf-8")
        cmd = [
            str(venv_py), "-I", str(worker),
            "--config", json.dumps(wcfg),
            "--requests", str(req_path),
            "--out", str(out_path),
        ]  # fmt: skip
        # The worker runs third-party model code: no secrets in its environment, and a scratch cwd
        # (not the project root, which holds .env and the state database).
        capture = owns_stderr()  # `live`/`jsonl` own stderr: the worker's lines are relayed, not written under them
        try:
            if capture:
                done = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", cwd=tmp,
                    env=config.worker_env(), timeout=timeout_s, check=False,
                )  # fmt: skip
                _relay(done.stdout)
                rc = done.returncode
            else:
                rc = subprocess.run(
                    cmd, stdout=_stderr_fd(), cwd=tmp, env=config.worker_env(), timeout=timeout_s, check=False
                ).returncode
            problem = None if rc == 0 else f"exit code {rc}"
        except subprocess.TimeoutExpired as exc:
            if capture:
                _relay(exc.output)
            problem = f"timed out after {timeout_s} s"
        out = _read_out(out_path)
        if problem is None and out is None:
            problem = "no output written"
        if problem and out is None:
            raise WorkerError(f"Worker for {spec.name} failed ({problem}); see its messages above.")
        if problem:  # workers save results as they go: keep what was done, the rest is reported per item
            stderr_line(f"warning: worker for {spec.name} stopped early ({problem}); using its partial results.", "WARNING")
        return out  # type: ignore[return-value]


# --- answers --------------------------------------------------------------------------------------


_TOL = 1e-3  # model probabilities may overshoot [0, 1] by float noise (bf16/fp16)


def _number(value: Any, what: str, lo: float, hi: float) -> float:
    """A finite float within [lo, hi] (+ float noise); NaN, inf and out-of-range values are a ValueError, so
    the item gets a per-item error and stays pending instead of being decided on a meaningless number."""
    x = float(value)
    if not math.isfinite(x) or x < lo - _TOL or x > hi + _TOL:
        raise ValueError(f"{what} must be a number in [{lo:g}, {hi:g}], got {x!r}")
    return x


def normalize_answer(question: dict, raw: dict) -> dict:
    """Model answer -> {type, pred, conf, ...}. Raises ValueError/KeyError/TypeError on an unusable answer."""
    t = question["type"]
    if t == "noul":
        p = _number(raw["noul"], "noul", 0.0, 1.0)
        return {"type": t, "p": p, "pred": p > 0.5, "conf": max(p, 1 - p)}
    if t == "choice":
        return {
            "type": t,
            "pred": raw["choice"],
            "conf": _number(raw["confidence"], "confidence", 0.0, 1.0),
            "probs": {k: _number(v, f"probability of {k!r}", 0.0, 1.0) for k, v in raw["probabilities"].items()},
        }
    top = len(question["criteria"]) - 1  # levels are 0..top
    probs = {
        int(k): _number(v, f"probability of level {k}", 0.0, 1.0) for k, v in raw["probabilities"].items()
    }
    if not probs or any(not 0 <= level <= top for level in probs):
        raise ValueError(f"probabilities must cover levels 0..{top}, got {sorted(probs)}")
    return {
        "type": t,
        "pred": max(probs, key=probs.get),  # type: ignore[arg-type]
        "score": _number(raw["score"], "score", 0.0, float(top)),
        "conf": _number(raw["confidence"], "confidence", 0.0, 1.0),
        "probs": probs,
    }


class DeciderBackend(Protocol):
    def decide(
        self, items: Sequence[TriageItem], q: QuestionSet
    ) -> tuple[dict[str, dict[str, dict]], dict[str, str]]:
        """(answers_by_rid, errors_by_rid). Every item lands in exactly one of them: an item is answered
        only when ALL enabled questions have a normalized answer, so a partial result never looks like a
        verdict. A WorkerError (nothing usable from the worker) propagates."""
        ...


class LocalWorkerBackend:
    def __init__(self, spec: ModelSpec, deciders_dir: Path, timeout_s: int, online: bool) -> None:
        self.spec = spec
        self.deciders_dir = deciders_dir
        self.timeout_s = timeout_s
        self.online = online

    def decide(
        self, items: Sequence[TriageItem], q: QuestionSet
    ) -> tuple[dict[str, dict[str, dict]], dict[str, str]]:
        if not items:
            return {}, {}
        requests = build_requests(items, q, self.spec.grouping)
        # request rid -> (item rid, question ids it answers); built from the items, never parsed from rids
        rid_map: dict[str, tuple[str, list[str]]] = {}
        for item in items:
            if self.spec.grouping == "joint":
                rid_map[request_id(item)] = (request_id(item), list(q.questions))
            else:
                for qid in q.questions:
                    rid_map[f"{request_id(item)}#{qid}"] = (request_id(item), [qid])
        out = run_worker(
            self.spec, requests, deciders_dir=self.deciders_dir, timeout_s=self.timeout_s, online=self.online
        )

        gathered: dict[str, dict[str, dict]] = {request_id(item): {} for item in items}
        errors: dict[str, str] = {}
        answered: set[str] = set()
        for res in out.get("results", []):
            rid = res.get("rid") if isinstance(res, dict) else None
            if rid not in rid_map:
                continue
            item_rid, qids = rid_map[rid]
            if "error" in res:
                errors.setdefault(item_rid, str(res["error"]))
                continue
            try:
                for qid in qids:
                    gathered[item_rid][qid] = normalize_answer(q.questions[qid], res["answers"][qid])
                answered.add(rid)
            except (KeyError, TypeError, ValueError) as exc:
                errors.setdefault(item_rid, f"malformed answer ({type(exc).__name__}: {exc})")
        # Requests run in order, so the first unanswered one is where the worker stopped: that item may be
        # the one that killed it. Items it never reached say nothing about themselves; both stay without a
        # verdict, only the wording of the error differs.
        in_progress: str | None = None
        for rid, (item_rid, _) in rid_map.items():
            if rid in answered or item_rid in errors:
                continue
            in_progress = in_progress or item_rid
            errors[item_rid] = (
                "no result from the model"
                if item_rid == in_progress
                else "not reached: the worker stopped earlier in the batch"
            )
        order = list(q.questions)
        answers = {
            item_rid: {qid: got[qid] for qid in order}
            for item_rid, got in gathered.items()
            if item_rid not in errors
        }
        return answers, errors


def make_backend(cfg: config.TriageConfig, models: dict[str, ModelSpec]) -> DeciderBackend:
    spec = models.get(cfg.model)
    if spec is None:
        raise ConfigError(f"unknown triage model {cfg.model!r}; available: {', '.join(models) or '(none)'}")
    if spec.remote and not cfg.allow_remote:
        raise ConfigError("remote decider backends are disabled (triage.allow_remote: false)")
    # triage.deciders_dir is relative to the repka home unless absolute (default: config.deciders_dir()).
    deciders_dir = config.project_root() / cfg.deciders_dir
    return LocalWorkerBackend(spec, deciders_dir, cfg.timeout_s, cfg.online)
