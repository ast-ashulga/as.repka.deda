import json

import pytest

from repka.progress.eta import RateEstimator, fmt_duration, history_medians
from repka.progress.events import NullReporter, RunProgress


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, dict]] = []

    def on_event(self, event, snapshot, payload):
        self.events.append((event, snapshot, payload))


def test_fmt_duration():
    assert fmt_duration(0) == "0s"
    assert fmt_duration(45.4) == "45s"
    assert fmt_duration(130) == "2m10s"
    assert fmt_duration(125) == "2m05s"
    assert fmt_duration(3900) == "1h05m"
    assert fmt_duration(-5) == "0s"


def test_eta_none_until_min_elapsed_and_done():
    clock = FakeClock()
    est = RateEstimator(clock)
    est.reset(20)
    assert est.eta_seconds(0, 20) is None  # nothing done yet
    clock.tick(2)
    est.update(5)
    assert est.eta_seconds(5, 20) is None  # under MIN_ELAPSED_S
    clock.tick(2)
    est.update(10)
    assert est.eta_seconds(10, 20) is not None
    assert est.eta_seconds(10, None) is None  # unknown total
    assert est.eta_seconds(20, 20) == 0.0


def test_eta_steady_rate():
    clock = FakeClock()
    est = RateEstimator(clock)
    est.reset(200)
    done = 0
    for _ in range(10):
        clock.tick(1)
        done += 10
        est.update(done)
    assert est.eta_seconds(done, 200) == pytest.approx(10, abs=1)


def test_ewma_follows_slowdown():
    clock = FakeClock()
    est = RateEstimator(clock)
    est.reset(210)
    done = 0
    for _ in range(10):  # 10 items/s
        clock.tick(1)
        done += 10
        est.update(done)
    for _ in range(10):  # then 1 item/s
        clock.tick(1)
        done += 1
        est.update(done)
    naive = (210 - done) / (done / 20)  # cumulative average rate
    assert est.eta_seconds(done, 210) > naive * 2


def make(plan, history=None, clock=None, sinks=()):
    clock = clock or FakeClock()
    return RunProgress(clock=clock, history=history, sinks=list(sinks), wall=lambda: 1_700_000_000.0), clock


def test_overall_eta_adds_history_for_later_stages():
    p, clock = make(None, history={"digest": 20.0})
    p.plan(["triage", "digest"])
    p.stage_start("triage", steps=1)
    p.step("dom", "repos", 100)
    for i in range(1, 6):
        clock.tick(2)
        p.advance(i * 10, 100)
    snap = p.snapshot()
    cur = snap["current"]["eta_seconds"]
    assert cur == pytest.approx(10, abs=1)  # 50 left at 5 items/s
    assert snap["overall"]["eta_seconds"] == pytest.approx(cur + 20)
    assert snap["overall"]["eta_is_lower_bound"] is False
    assert snap["overall"]["eta_at"] == pytest.approx(1_700_000_000.0 + cur + 20)


def test_overall_is_lower_bound_when_a_later_stage_has_no_history():
    p, clock = make(None, history={})
    p.plan(["triage", "digest"])
    p.stage_start("triage", steps=1)
    p.step("dom", "repos", 100)
    clock.tick(5)
    p.advance(25, 100)
    overall = p.snapshot()["overall"]
    assert overall["eta_seconds"] is not None and overall["eta_seconds"] > 0
    assert overall["eta_is_lower_bound"] is True


def test_overall_unknown_when_nothing_known():
    p, clock = make(None)
    p.plan(["collect", "digest"])
    p.stage_start("collect")
    clock.tick(1)
    overall = p.snapshot()["overall"]
    assert overall["eta_seconds"] is None and overall["eta_at"] is None


def test_stage_eta_uses_mean_step_time_for_remaining_steps():
    p, clock = make(None)
    p.plan(["triage"])
    p.stage_start("triage", steps=4)
    p.step("a", "repos", 10)
    clock.tick(10)
    p.advance(10, 10)  # step a: 10 s
    p.step("b", "repos", 10)
    clock.tick(5)
    p.advance(5, 10)  # b halfway after 5 s -> 5 s left
    eta = p.snapshot()["current"]["eta_seconds"]
    assert eta == pytest.approx(5 + 2 * 10, abs=1.5)  # rest of b + two more steps at the 10 s mean


def test_history_medians_ignores_old_rows_and_idle_stages():
    def row(**timings):
        return json.dumps({"timings": {k: {"seconds": s, "items": n} for k, (s, n) in timings.items()}})

    rows = [
        None,
        "{}",
        "not json",
        row(triage=(0.1, 0)),  # idle stage: not a sample
        row(triage=(100, 10), digest=(2, 1)),
        row(triage=(300, 10)),
        row(triage=(200, 10)),
    ]
    med = history_medians(rows)
    assert med["triage"] == 200
    assert med["digest"] == 2


def test_history_medians_uses_only_last_five_working_runs():
    rows = [json.dumps({"timings": {"x": {"seconds": s, "items": 1}}}) for s in (1, 1, 1, 1, 1, 100, 100, 100)]
    assert history_medians(rows)["x"] == 1  # newest first: only the first five count


def test_timings_sum_items_per_stage():
    p, clock = make(None)
    p.plan(["triage"])
    p.stage_start("triage", steps=2)
    p.step("a", "repos", 10)
    clock.tick(4)
    p.advance(10, 10)
    p.step("b", "repos", 5)
    clock.tick(6)
    p.advance(5, 5)
    p.stage_end("ok")
    assert p.timings() == {"triage": {"seconds": 10.0, "items": 15}}


def test_skipped_stage_is_not_a_timing_and_finish_closes_running_stage():
    p, clock = make(None)
    p.plan(["collect", "triage"])
    p.stage_start("collect")
    p.stage_end("skipped", "nothing due")
    p.stage_start("triage", steps=1)
    clock.tick(3)
    p.finish("failed")
    assert "collect" not in p.timings()
    assert p.snapshot()["status"] == "failed"
    assert [s["status"] for s in p.snapshot()["stages"]] == ["skipped", "error"]
    p.finish("failed")  # idempotent


def test_advance_without_step_creates_one_and_events_fan_out():
    rec = Recorder()
    p, clock = make(None, sinks=[rec])
    p.plan(["embed"])
    p.stage_start("embed", steps=1)
    p.advance(1, 4, "batch")
    p.note("hello")
    p.stage_end("ok")
    p.finish("ok")
    names = [e for e, _, _ in rec.events]
    assert names == ["plan", "stage_start", "step", "progress", "note", "stage_end", "finish"]
    assert rec.events[4][2] == {"message": "hello"}
    assert rec.events[3][1]["current"]["done"] == 1


def test_sink_error_is_swallowed():
    class Boom:
        def on_event(self, *a):
            raise RuntimeError("renderer broke")

    p, _ = make(None, sinks=[Boom()])
    p.plan(["digest"])
    p.stage_start("digest")
    p.stage_end("ok")
    p.finish("ok")  # must not raise


def test_null_reporter_accepts_everything():
    r = NullReporter()
    r.plan(["a"])
    r.stage_start("a", steps=1)
    r.step("x", "u", 1)
    r.advance(1, 1, "d")
    r.stage_end("ok")
    r.note("n")
    r.finish("ok")
