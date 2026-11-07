import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from repka import config
from repka.config import ConfigError, TriageConfig
from repka.models import TriageItem
from repka.triage.deciders import (
    LocalWorkerBackend,
    ModelSpec,
    QuestionSet,
    WorkerError,
    build_requests,
    load_models,
    load_questions,
    make_backend,
    merge_questions,
    normalize_answer,
    questions_hash,
    run_worker,
)

FIXTURES = Path(__file__).parent / "fixtures" / "deciders"
SHA = "a" * 40


def spec(mode: str = "ok", grouping: str = "joint", remote: bool = False) -> ModelSpec:
    # fake_worker.py takes its behaviour from `repo` when the config has no `mode`
    return ModelSpec("fake", "fake_worker", "venv", mode, SHA, grouping, "auto", "auto", remote)  # type: ignore[arg-type]


@pytest.fixture
def deciders_dir(tmp_path):
    d = tmp_path / "deciders"
    (d / "venv" / "bin").mkdir(parents=True)
    (d / "venv" / "bin" / "python").symlink_to(sys.executable)
    (d / "workers").mkdir()
    shutil.copy(FIXTURES / "fake_worker.py", d / "workers" / "fake_worker.py")
    return d


def qset() -> QuestionSet:
    return load_questions(config.deciders_dir() / "questions.yaml")


def items(n: int = 2) -> list[TriageItem]:
    return [TriageItem(repo_id=100 + i, domain_id="mcp", state=f"state {i}", input_hash=f"h{i}") for i in range(n)]


def test_revision_must_be_40_hex(tmp_path):
    path = tmp_path / "models.yaml"
    entry = "models:\n  m:\n    runner: r\n    venv: v\n    repo: org/m\n    revision: {rev}\n"
    path.write_text(entry.format(rev=SHA))
    assert load_models(path)["m"].revision == SHA
    assert load_models(path)["m"].remote is False
    for bad in ("main", "abc123", "A" * 40, "a" * 39):
        path.write_text(entry.format(rev=bad))
        with pytest.raises(ConfigError, match="40-character"):
            load_models(path)
    assert "d1-3B" in load_models(config.deciders_dir() / "models.yaml")


def test_load_questions_skips_disabled():
    q = qset()
    assert list(q.questions) == ["relevant", "substance", "kind"]
    assert q.questions["relevant"]["type"] == "noul"
    assert "{readme_excerpt}" in q.state_template


def test_merge_questions_override_add_remove():
    base = qset()
    merged = merge_questions(
        base,
        {
            "relevant": {"type": "noul", "instructions": "Is it about MCP?"},
            "extra": {"type": "noul", "instructions": "Is it maintained?"},
            "kind": {"enabled": False},
            "nope": {"enabled": False},
        },
    )
    assert list(merged.questions) == ["relevant", "substance", "extra"]
    assert merged.questions["relevant"]["instructions"] == "Is it about MCP?"
    assert merged.state_template == base.state_template
    with pytest.raises(ConfigError):
        merge_questions(base, {"x": {"type": "bogus"}})


def test_questions_hash_changes_with_instructions():
    base = qset()
    assert questions_hash(base) == questions_hash(qset())
    assert len(questions_hash(base)) == 16
    changed = merge_questions(base, {"relevant": {"type": "noul", "instructions": "different"}})
    assert questions_hash(changed) != questions_hash(base)


def test_build_requests_joint_vs_single():
    q = qset()
    joint = build_requests(items(2), q, "joint")
    assert [r["rid"] for r in joint] == ["mcp:100", "mcp:101"]
    assert list(joint[0]["questions"]) == ["relevant", "substance", "kind"]
    assert joint[0]["state"] == "state 0"
    assert joint[0]["questions"]["substance"]["criteria"][0].startswith("empty")
    single = build_requests(items(2), q, "single")
    assert [r["rid"] for r in single] == [f"mcp:{i}#{qid}" for i in (100, 101) for qid in q.questions]
    assert all(len(r["questions"]) == 1 for r in single)


def test_normalize_noul_choice_score():
    q = qset().questions
    results = json.loads((FIXTURES / "out_d1.json").read_text())["results"]
    assert len(results) == 3
    answers = results[0]["answers"]
    noul = normalize_answer(q["relevant"], answers["relevant"])
    assert noul["type"] == "noul" and noul["pred"] is True
    assert noul["p"] == pytest.approx(0.9579, abs=1e-3) and noul["conf"] == noul["p"]
    score = normalize_answer(q["substance"], answers["substance"])
    assert score["pred"] == 4 and score["score"] == pytest.approx(3.63, abs=1e-2)
    assert set(score["probs"]) == {0, 1, 2, 3, 4}
    choice = normalize_answer(q["kind"], answers["kind"])
    assert choice["pred"] == "tool" and choice["conf"] == pytest.approx(0.8047, abs=1e-3)
    assert choice["probs"]["library"] < choice["conf"]
    low = normalize_answer(q["relevant"], results[1]["answers"]["relevant"])
    assert low["pred"] is False and low["conf"] == pytest.approx(1 - low["p"])


def test_run_worker_scratch_cwd_and_env_without_secrets(deciders_dir, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "s3cret-gh")
    monkeypatch.setenv("REPKA_TG_API_HASH", "s3cret-tg")
    requests = build_requests(items(1), qset(), "joint")
    out = run_worker(spec(), requests, deciders_dir=deciders_dir, timeout_s=60, online=False)
    meta = out["meta"]
    cwd = os.path.realpath(meta["cwd"])
    assert cwd != os.path.realpath(config.project_root())
    assert cwd == os.path.realpath(meta["requests_dir"])  # the scratch dir that holds requests.json
    assert "GITHUB_TOKEN" not in meta["env"] and "REPKA_TG_API_HASH" not in meta["env"]
    assert not any("s3cret" in v for v in meta["env"].values())
    assert meta["config"]["online"] is False and meta["config"]["revision"] == SHA
    assert out["results"][0]["rid"] == "mcp:100"


def test_partial_out_json_used_on_crash(deciders_dir, capsys):
    requests = build_requests(items(3), qset(), "joint")
    out = run_worker(spec("crash_after_1"), requests, deciders_dir=deciders_dir, timeout_s=60, online=False)
    assert [r["rid"] for r in out["results"]] == ["mcp:100"]
    assert "stopped early" in capsys.readouterr().err

    # through the backend: the finished item is answered, the others stay without a verdict
    backend = LocalWorkerBackend(spec("crash_after_1"), deciders_dir, 60, False)
    answers, errors = backend.decide(items(3), qset())
    assert list(answers) == ["mcp:100"]
    assert set(errors) == {"mcp:101", "mcp:102"}
    assert "no result" in errors["mcp:101"] and "not reached" in errors["mcp:102"]


def test_timeout_without_output_raises_worker_error(deciders_dir):
    backend = LocalWorkerBackend(spec("sleep"), deciders_dir, 1, False)
    with pytest.raises(WorkerError, match="timed out"):
        backend.decide(items(1), qset())


def test_remote_backend_refused_unless_allowed():
    models = {"far": spec(remote=True), "near": spec()}
    with pytest.raises(ConfigError, match=r"remote decider backends are disabled \(triage.allow_remote: false\)"):
        make_backend(TriageConfig(model="far"), models)
    with pytest.raises(ConfigError, match="unknown triage model"):
        make_backend(TriageConfig(model="ghost"), models)
    assert make_backend(TriageConfig(model="far", allow_remote=True), models).spec.name == "fake"
    backend = make_backend(TriageConfig(model="near", timeout_s=5, online=True), models)
    assert backend.timeout_s == 5 and backend.online is True
    assert backend.deciders_dir == config.deciders_dir()


def test_single_grouping_rejoins_answers_per_item(deciders_dir):
    q = qset()
    backend = LocalWorkerBackend(spec(grouping="single"), deciders_dir, 60, False)
    answers, errors = backend.decide(items(2), q)
    assert errors == {}
    assert list(answers) == ["mcp:100", "mcp:101"]
    assert list(answers["mcp:100"]) == list(q.questions)
    assert answers["mcp:100"]["relevant"]["pred"] is True
    assert answers["mcp:101"]["substance"]["pred"] == 4


def test_malformed_answer_is_an_error_not_a_verdict(deciders_dir):
    backend = LocalWorkerBackend(spec("bad_answer"), deciders_dir, 60, False)
    answers, errors = backend.decide(items(2), qset())
    assert list(answers) == ["mcp:101"]
    assert "malformed answer" in errors["mcp:100"]
