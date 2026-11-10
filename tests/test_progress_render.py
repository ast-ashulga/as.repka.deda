import io
import json
import logging

import pytest

from repka.logging_setup import setup_logging
from repka.progress.active import activate, owns_stderr, stderr_line
from repka.progress.events import RunProgress
from repka.progress.render import JsonlRenderer, LinesRenderer, LiveRenderer, make_renderer

WALL = 1_700_000_000.0


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def build(renderer_factory, history=None):
    clock = Clock()
    out = io.StringIO()
    renderer = renderer_factory(out, clock)
    p = RunProgress(clock=clock, history=history, sinks=[renderer], wall=lambda: WALL)
    return p, renderer, out, clock


def lines_of(out):
    return [ln for ln in out.getvalue().splitlines() if ln]


def lines_renderer(out, clock):
    return LinesRenderer(out, clock, lambda: WALL)


def jsonl_renderer(out, clock):
    return JsonlRenderer(out, clock, lambda: WALL)


def live_renderer(out, clock, columns=100, unicode=True):
    return LiveRenderer(out, clock, lambda: WALL, columns=lambda: columns, unicode=unicode)


def test_lines_format_and_throttle():
    p, _, out, clock = build(lines_renderer, history={"digest": 30.0})
    p.plan(["collect", "triage", "digest"])
    p.stage_start("triage", steps=2)
    p.step("ai-agent", "repos", 1200)
    n_before = len(lines_of(out))
    for i in range(1, 11):  # ten advances within one second: at most one extra line
        clock.now += 0.1
        p.advance(i * 10, 1200)
    assert len(lines_of(out)) - n_before <= 1
    clock.now += 12
    p.advance(400, 1200)
    last = lines_of(out)[-1]
    assert "2/3 triage" in last and "ai-agent" in last and "400/1200 repos" in last and "33%" in last
    assert "eta" in last
    assert "(1/2)" in last  # step counter of the stage


def test_lines_reports_stage_end_and_final_line():
    p, _, out, clock = build(lines_renderer)
    p.plan(["digest"])
    p.stage_start("digest")
    clock.now += 4
    p.stage_end("ok", "preview of 3 item(s)")
    p.finish("ok")
    text = out.getvalue()
    assert "digest ok in 4s" in text and "preview of 3 item(s)" in text
    assert "run ok" in lines_of(out)[-1]


def test_lines_unknown_and_lower_bound_eta_wording():
    p, _, out, clock = build(lines_renderer)
    p.plan(["collect", "digest"])
    p.stage_start("collect")
    assert "eta ?" in lines_of(out)[-1]
    p.step("d search", "queries", 10)
    clock.now += 5
    p.advance(5, 10)  # in-step eta known, digest has no history: lower bound
    clock.now += 11
    p.advance(6, 10)
    assert "eta >" in lines_of(out)[-1]


def test_jsonl_every_line_is_json():
    p, renderer, out, clock = build(jsonl_renderer)
    p.plan(["triage"])
    p.stage_start("triage", steps=1)
    p.step("d", "repos", 10)
    clock.now += 5
    p.advance(5, 10, "readme 5/10")
    p.note("a note")
    with activate(renderer):
        stderr_line("run #3: 1 domain(s), send=no")
        logging.getLogger("repka.x").warning("careful")  # no repka handler installed here: ignored
    renderer.log_line("raw log", "WARNING")
    p.stage_end("ok")
    p.finish("ok")
    objs = [json.loads(ln) for ln in lines_of(out)]  # every line parses
    events = [o["event"] for o in objs]
    assert events[:4] == ["plan", "stage_start", "step", "progress"]
    assert {"note", "log", "stage_end", "summary"} <= set(events)
    progress = next(o for o in objs if o["event"] == "progress")
    assert progress["current"]["done"] == 5 and progress["overall"]["eta_seconds"] is not None
    assert all("ts" in o for o in objs)
    log_msgs = [o["message"] for o in objs if o["event"] == "log"]
    assert "run #3: 1 domain(s), send=no" in log_msgs and "raw log" in log_msgs


def test_jsonl_progress_throttled_to_one_per_second():
    p, _, out, clock = build(jsonl_renderer)
    p.plan(["embed"])
    p.stage_start("embed", steps=1)
    p.step("d", "repos", 100)
    for i in range(1, 21):
        clock.now += 0.1  # 2 s in total
        p.advance(i, 100)
    count = sum(1 for ln in lines_of(out) if json.loads(ln)["event"] == "progress")
    assert 1 <= count <= 3


def test_jsonl_strips_control_characters_from_outside_text():
    p, _, out, _ = build(jsonl_renderer)
    p.plan(["triage"])
    p.stage_start("triage", steps=1)
    p.step("evil\x1b[31m-domain", "repos", 1)
    p.note("bad\x1b]0;title\x07 note")
    assert "\x1b" not in out.getvalue() and "\\u001b" not in out.getvalue()


def test_live_redraws_single_line_and_prints_finished_stage_permanently():
    p, _, out, clock = build(live_renderer)
    p.plan(["triage", "digest"])
    p.stage_start("triage", steps=1)
    p.step("ai-agent", "repos", 100)
    clock.now += 5
    p.advance(25, 100)
    raw = out.getvalue()
    assert "\r" in raw and "\x1b[2K" in raw
    assert "ai-agent" in raw and "25/100" in raw and "25%" in raw and "█" in raw
    clock.now += 5
    p.advance(50, 100)
    p.stage_end("ok", "50 decided")
    permanent = [ln for ln in raw_lines(out) if "✓ triage" in ln and "50 decided" in ln]
    assert permanent, out.getvalue()


def raw_lines(out):
    return out.getvalue().replace("\r", "\n").replace("\x1b[2K", "").splitlines()


def test_live_trims_to_columns():
    p, renderer, out, clock = build(lambda o, c: live_renderer(o, c, columns=40))
    p.plan(["triage"])
    p.stage_start("triage", steps=1)
    p.step("a-very-long-domain-name-that-will-not-fit-anywhere", "repos", 100)
    clock.now += 5
    p.advance(10, 100)
    drawn = out.getvalue().split("\r")[-1].replace("\x1b[2K", "")
    assert len(drawn) <= 39


def test_live_log_line_clears_then_redraws():
    p, renderer, out, clock = build(live_renderer)
    p.plan(["triage"])
    p.stage_start("triage", steps=1)
    p.step("d", "repos", 10)
    clock.now += 5
    p.advance(2, 10)
    before = len(out.getvalue())
    renderer.log_line("WARNING something happened", "WARNING")
    tail = out.getvalue()[before:]
    assert tail.startswith("\r\x1b[2K") and "WARNING something happened\n" in tail
    assert "▶" in tail.split("happened\n", 1)[1]  # the status line is redrawn after the log line


def test_live_ascii_bar_when_not_unicode():
    p, _, out, clock = build(lambda o, c: live_renderer(o, c, unicode=False))
    p.plan(["triage"])
    p.stage_start("triage", steps=1)
    p.step("d", "repos", 10)
    clock.now += 5
    p.advance(5, 10)
    raw = out.getvalue()
    assert "█" not in raw and "#" in raw


def test_live_finish_ends_line_with_newline():
    p, _, out, clock = build(live_renderer)
    p.plan(["triage"])
    p.stage_start("triage", steps=1)
    p.step("d", "repos", 10)
    clock.now += 5
    p.advance(5, 10)
    p.finish("failed")  # stage still open: interrupted
    assert out.getvalue().endswith("\n")
    assert "failed" in raw_lines(out)[-1]


def test_make_renderer_live_on_non_tty_falls_back_to_lines():
    out = io.StringIO()
    renderer = make_renderer("live", out, is_tty=False, clock=Clock(), wall=lambda: WALL)
    assert isinstance(renderer, LinesRenderer)
    p = RunProgress(clock=Clock(), sinks=[renderer], wall=lambda: WALL)
    p.plan(["digest"])
    p.stage_start("digest")
    p.stage_end("ok")
    p.finish("ok")
    assert "\x1b" not in out.getvalue() and "\r" not in out.getvalue()


def test_make_renderer_modes():
    out, clock = io.StringIO(), Clock()
    assert make_renderer("plain", out, is_tty=True, clock=clock, wall=lambda: WALL) is None
    assert isinstance(make_renderer("jsonl", out, is_tty=True, clock=clock, wall=lambda: WALL), JsonlRenderer)
    assert isinstance(make_renderer("live", out, is_tty=True, clock=clock, wall=lambda: WALL), LiveRenderer)
    with pytest.raises(ValueError):
        make_renderer("fancy", out, is_tty=True, clock=clock, wall=lambda: WALL)


def test_active_stderr_line_plain_goes_to_stderr(capsys):
    assert not owns_stderr()
    stderr_line("hello")
    assert capsys.readouterr().err == "hello\n"
    out = io.StringIO()
    with activate(LinesRenderer(out, Clock(), lambda: WALL)):  # lines does not own stderr
        stderr_line("again")
    assert capsys.readouterr().err == "again\n"


def test_activate_restores_previous(capsys):
    out = io.StringIO()
    with activate(JsonlRenderer(out, Clock(), lambda: WALL)):
        assert owns_stderr()
    assert not owns_stderr()


def test_logging_handler_routes_through_active_jsonl(home, capsys):
    setup_logging("INFO")
    out = io.StringIO()
    log = logging.getLogger("repka.test")
    with activate(JsonlRenderer(out, Clock(), lambda: WALL)):
        log.warning("worker hiccup")
    log.warning("outside")
    objs = [json.loads(ln) for ln in lines_of(out)]
    assert len(objs) == 1 and objs[0]["event"] == "log" and "worker hiccup" in objs[0]["message"]
    assert objs[0]["level"] == "WARNING"
    err = capsys.readouterr().err
    assert "outside" in err and "worker hiccup" not in err


def test_live_ticker_redraw_moves_elapsed_and_eta_between_events():
    p, renderer, out, clock = build(live_renderer, history={"triage": 100.0})
    p.plan(["triage"])
    p.stage_start("triage", steps=1)
    p.step("d", "repos", 100)
    clock.now += 5
    p.advance(10, 100)
    first = out.getvalue().split("\r")[-1]
    assert "| 5s |" in first
    clock.now += 30  # a long chunk: no events, only the ticker redraws
    renderer.redraw()
    later = out.getvalue().split("\r")[-1]
    assert "| 35s |" in later and later != first


def test_live_finish_stops_the_ticker():
    out, clock = io.StringIO(), Clock()
    renderer = LiveRenderer(out, clock, lambda: WALL, columns=lambda: 100, unicode=True, ticker=True)
    p = RunProgress(clock=clock, sinks=[renderer], wall=lambda: WALL)
    p.plan(["digest"])
    p.stage_start("digest")
    assert renderer._ticker is not None
    p.stage_end("ok")
    p.finish("ok")
    assert renderer._ticker is None
