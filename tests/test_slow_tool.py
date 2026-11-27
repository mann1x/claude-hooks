"""A tool call that runs long is reported, never silent.

Tools run in the agent loop's thread and nothing can interrupt them; two
council researcher greps ran for over an hour with no event while the run
looked dead (csl-2026-11-27-0507-da28). ``execute_tool_calls`` now logs a
warning and calls ``on_slow`` at each of ``SLOW_TOOL_NOTICE_S``; ``run_loop``
routes it to ``on_guard``, which the council records as a runtime event.
"""
from __future__ import annotations

import time

from claude_hooks.agent_loop import runner


def _call(name="grep", args='{"pattern": "x"}'):
    return [{"id": "c1", "function": {"name": name, "arguments": args}}]


def test_a_slow_tool_is_reported(monkeypatch):
    monkeypatch.setattr(runner, "SLOW_TOOL_NOTICE_S", (0.05,))
    events: list[dict] = []

    def slow(name, args, cwd):
        time.sleep(0.3)
        return "done"

    out = runner.execute_tool_calls(_call(), "/tmp", {}, slow, on_slow=events.append)
    assert out[0]["content"] == "done"
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "slow_tool" and ev["tool"] == "grep"
    assert ev["args"] == '{"pattern": "x"}' and ev["elapsed_s"] >= 0.05


def test_a_fast_tool_is_not(monkeypatch):
    monkeypatch.setattr(runner, "SLOW_TOOL_NOTICE_S", (0.2,))
    events: list[dict] = []
    runner.execute_tool_calls(_call(), "/tmp", {}, lambda *a: "ok", on_slow=events.append)
    time.sleep(0.3)
    assert events == []


def test_a_failing_tool_stops_its_watch(monkeypatch):
    monkeypatch.setattr(runner, "SLOW_TOOL_NOTICE_S", (0.2,))
    events: list[dict] = []

    def boom(*a):
        raise OSError("disk gone")

    try:
        runner.execute_tool_calls(_call(), "/tmp", {}, boom, on_slow=events.append)
    except OSError:
        pass
    time.sleep(0.3)
    assert events == []


def test_run_loop_routes_the_notice_to_on_guard(monkeypatch):
    monkeypatch.setattr(runner, "SLOW_TOOL_NOTICE_S", (0.05,))
    guard: list[dict] = []
    replies = [
        {"choices": [{"message": {"role": "assistant", "content": "",
                                  "tool_calls": _call()},
                      "finish_reason": "tool_calls"}]},
        {"choices": [{"message": {"role": "assistant", "content": "final report"},
                      "finish_reason": "stop"}]},
    ]

    def slow(name, args, cwd):
        time.sleep(0.3)
        return "result"

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]},
        cwd="/tmp",
        config=runner.LoopConfig(force_first_tool_call=False, on_guard=guard.append),
        tool_specs=[{"type": "function", "function": {
            "name": "grep", "parameters": {"type": "object", "properties": {}}}}],
        chat_fn=lambda p: replies.pop(0),
        tool_executor=slow,
    )
    assert out["choices"][0]["message"]["content"] == "final report"
    assert any(e.get("kind") == "slow_tool" and e.get("tool") == "grep" for e in guard)


def test_events_no_follow_exits_when_the_stream_goes_quiet(monkeypatch, capsys):
    """``claude-consultants events --no-follow``: a quick status read of a
    running council, which otherwise holds the stream open to its end."""
    import argparse
    from consultants import cli

    lines = [b"event: node_exit\n",
             b'data: {"kind": "node_exit", "role": "researcher", "lane_idx": 4}\n', b"\n"]
    seen: dict = {}

    class Quiet:
        def readline(self):
            if lines:
                return lines.pop(0)
            raise TimeoutError("timed out")

    def fake_urlopen(req, timeout):
        seen["timeout"] = timeout
        return Quiet()

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    args = argparse.Namespace(sid="csl-1", since=None, kinds=None, milestones=True, no_follow=True)
    assert cli.cmd_events(args, "http://engine") == 0
    assert seen["timeout"] == cli.EVENTS_IDLE_EXIT_S
    assert "node_exit" in capsys.readouterr().out


# A lane can leave a running tool on wrap-up or cancel (it used to wait for
# the tool, however long: an engine restart was the only way out).
import threading  # noqa: E402

from claude_hooks import tool_stop  # noqa: E402


def _two_calls():
    return [{"id": "c1", "function": {"name": "grep", "arguments": '{"pattern": "a"}'}},
            {"id": "c2", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]


def test_a_stop_leaves_the_running_tool_and_skips_the_rest(monkeypatch):
    monkeypatch.setattr(runner, "TOOL_STOP_POLL_S", 0.05)
    ran: list[str] = []
    saw_stop = threading.Event()
    t_stop = time.monotonic() + 0.2

    def tool(name, args, cwd):
        ran.append(name)
        for _ in range(200):  # ~10 s unless asked to stop
            if tool_stop.requested():
                saw_stop.set()
                return "partial"
            time.sleep(0.05)
        return "full"

    recorded: list[tuple] = []
    t0 = time.monotonic()
    out = runner.execute_tool_calls(
        _two_calls(), "/tmp", {}, tool,
        on_tool=lambda n, a, o, ms, err: recorded.append((n, err)),
        stop_check=lambda: time.monotonic() > t_stop)
    assert time.monotonic() - t0 < 2
    assert [m["content"] for m in out] == [runner.TOOL_STOPPED_ANSWER] * 2
    assert ran == ["grep"]  # the second call was never run
    assert recorded == [("grep", "stopped"), ("read_file", "stopped")]
    assert saw_stop.wait(2)  # the worker was told, and quit


def test_without_a_stop_check_the_tool_runs_inline():
    threads: list = []
    runner.execute_tool_calls(_call(), "/tmp", {},
                              lambda *a: threads.append(threading.current_thread()) or "ok")
    assert threads == [threading.current_thread()]


def test_a_stoppable_tool_error_reaches_the_lane(monkeypatch):
    monkeypatch.setattr(runner, "TOOL_STOP_POLL_S", 0.05)

    def boom(*a):
        raise OSError("disk gone")

    try:
        runner.execute_tool_calls(_call(), "/tmp", {}, boom, stop_check=lambda: False)
    except OSError as e:
        assert "disk gone" in str(e)
    else:
        raise AssertionError("the tool's error was swallowed")


def test_grep_quits_when_its_caller_stops(tmp_path):
    import json
    from claude_hooks.caliber_proxy import tools
    (tmp_path / "f.txt").write_text("needle\n")
    halt = threading.Event()
    halt.set()
    import contextvars
    out = contextvars.copy_context().run(
        lambda: (tool_stop.bind(halt), tools.execute(
            "grep", json.dumps({"pattern": "needle"}), str(tmp_path)))[1])
    assert out == "(search stopped: the run is wrapping up)"
    # Outside that context nothing is stopped.
    assert tools.execute("grep", json.dumps({"pattern": "needle"}), str(tmp_path)) == "f.txt:1: needle"


def test_a_lanes_stop_check_covers_cancel_as_well_as_wrap_up():
    from types import SimpleNamespace
    from consultants.engine import graph
    rc = SimpleNamespace(wrapping_up=False, cancelled=False)
    check = graph._wrap_up_check(SimpleNamespace(run_control=rc))
    assert check() is False
    rc.cancelled = True
    assert check() is True
    rc.cancelled, rc.wrapping_up = False, True
    assert check() is True


def test_run_loop_leaves_a_stuck_tool_on_wrap_up(monkeypatch):
    monkeypatch.setattr(runner, "TOOL_STOP_POLL_S", 0.05)
    wrapping = threading.Event()
    replies = [
        {"choices": [{"message": {"role": "assistant", "content": "",
                                  "tool_calls": _call()}, "finish_reason": "tool_calls"}]},
        {"choices": [{"message": {"role": "assistant", "content": "report from what I had"},
                      "finish_reason": "stop"}]},
    ]

    def stuck(name, args, cwd):
        wrapping.set()  # the operator asks for wrap-up while the tool runs
        time.sleep(10)
        return "never seen"

    t0 = time.monotonic()
    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]},
        cwd="/tmp",
        config=runner.LoopConfig(force_first_tool_call=False, wrap_up_check=wrapping.is_set),
        tool_specs=[{"type": "function", "function": {
            "name": "grep", "parameters": {"type": "object", "properties": {}}}}],
        chat_fn=lambda p: replies.pop(0),
        tool_executor=stuck,
    )
    assert time.monotonic() - t0 < 3
    assert out["choices"][0]["message"]["content"] == "report from what I had"
