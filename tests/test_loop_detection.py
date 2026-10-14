"""Plan D10 (loop guard) and D14 (mistake limit)."""
from __future__ import annotations

import json

from claude_hooks import loop_detection as ld
from claude_hooks.agent_loop import runner


def _args(**kw):
    return json.dumps(kw)


# ---- the tracker ---------------------------------------------------- #

def test_consecutive_identical_warns_at_2_and_is_hard_at_5():
    t = ld.LoopTracker()
    kinds = []
    for _ in range(5):
        v = t.inspect("read_file", _args(path="a.py"))
        kinds.append(v.kind)
        t.note_outcome(True, f"body {len(kinds)}")   # answers differ
    assert kinds == ["ok", "soft", "ok", "ok", "hard"]


def test_signature_ignores_key_order():
    t = ld.LoopTracker()
    t.inspect("grep", '{"a": 1, "b": 2}')
    t.note_outcome(True, "x1")
    assert t.inspect("grep", '{"b": 2, "a": 1}').kind == "soft"


def test_a_failing_call_counts_down_across_interleaved_calls():
    t = ld.LoopTracker()
    msgs = []
    for k in range(7):
        v = t.inspect("edit_file", _args(path="a.py", old="x"))
        msgs.append(v)
        if v.kind != "hard":
            t.note_outcome(False, "error: old text not found")
        t.inspect("read_file", _args(path=f"f{k}.py"))
        t.note_outcome(True, f"body {k}")
    assert msgs[0].kind == "ok"
    assert [m.kind for m in msgs[1:6]] == ["soft"] * 5
    assert "5 strikes left" in msgs[1].message
    assert "LAST strike" in msgs[5].message
    assert msgs[6].kind == "hard" and msgs[6].rule == "barren"


def test_a_success_clears_the_failure_tally():
    t = ld.LoopTracker()
    for _ in range(3):
        t.inspect("run_tests", _args(k="x"))
        t.note_outcome(False, "error: 1 failed")
        t.inspect("read_file", _args(path="a.py"))
        t.note_outcome(True, "a")
    t.inspect("run_tests", _args(k="x"))
    t.note_outcome(True, "ok")
    t.inspect("read_file", _args(path="b.py"))
    t.note_outcome(True, "b")
    assert t.barren == {}


def test_same_answer_cycle_is_soft_for_reads_hard_for_writes():
    read, write = ld.LoopTracker(), ld.LoopTracker()
    rk, wk = [], []
    for k in range(6):
        rk.append(read.inspect("grep", _args(pattern="x")).kind)
        read.note_outcome(True, "same answer")
        read.inspect("read_file", _args(path=f"{k}.py"))
        read.note_outcome(True, f"b{k}")
        wk.append(write.inspect("write_file", _args(path="a.py", body="y")).kind)
        write.note_outcome(True, "wrote a.py, now revision #%d" % k)
        write.inspect("read_file", _args(path=f"{k}.py"))
        write.note_outcome(True, f"b{k}")
    assert rk == ["ok", "ok", "soft", "soft", "soft", "soft"]
    # Revision ordinals are not a new answer.
    assert wk == ["ok", "ok", "soft", "soft", "soft", "hard"]


def test_back_to_back_same_answer_still_reaches_hard():
    # The fork only warns here; with no call in between there was no edit.
    t = ld.LoopTracker()
    kinds = []
    for _ in range(5):
        kinds.append(t.inspect("grep", _args(pattern="x")).kind)
        t.note_outcome(True, "same")
    assert kinds == ["ok", "soft", "soft", "soft", "hard"]


def test_polling_tools_are_exempt():
    t = ld.LoopTracker(polling=frozenset({"council_status"}))
    assert all(t.inspect("council_status", "{}").kind == "ok" for _ in range(10))


def test_failed_output_convention():
    assert ld.is_failed_output("error: path not found: x")
    assert ld.is_failed_output('  {"success": false, "why": "x"}')
    assert not ld.is_failed_output("1 | def error_handler():")
    assert ld.describe_call("read_file", '{"path": "src/a.py"}') == \
        '`read_file` {path: "src/a.py"}'


def test_mistake_limit():
    m = ld.MistakeLimit()
    assert not any(m.note(True) for _ in range(5))
    assert m.note(True)
    m.note(False)
    assert m.streak == 0


# ---- run_loop ------------------------------------------------------- #

def _turn(name, args, cid):
    return {"choices": [{"message": {"role": "assistant", "content": "",
                                     "tool_calls": [{"id": cid, "type": "function",
                                                     "function": {"name": name,
                                                                  "arguments": args}}]},
                         "finish_reason": "tool_calls"}]}


def _final(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def _run(script, executor, **cfg):
    sent, events, ran = [], [], []

    def chat_fn(payload):
        sent.append(dict(payload, messages=list(payload["messages"])))
        return script.pop(0)

    def tool(name, args, cwd):
        ran.append((name, args))
        return executor(name, args)

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False, max_iterations=20,
                                 force_answer_after=0, on_guard=events.append, **cfg),
        tool_specs=[], chat_fn=chat_fn, tool_executor=tool)
    return out, sent, events, ran


def test_run_loop_repeated_read_ends_with_a_named_report():
    a = _args(path="src/a.py")
    script = [_turn("read_file", a, f"c{k}") for k in range(6)] + [_final("Report.")]
    out, sent, events, ran = _run(script, lambda n, x: "body")
    assert out["_stop_reason"] == "loop_guard"
    assert out["_failure_class"] == "loop_guard"
    assert out["_loop_call"] == '`read_file` {path: "src/a.py"}'
    assert out["choices"][0]["message"]["content"] == "Report."
    # Call 2 is the consecutive warning, 3-4 the same-answer cycle, 5 the
    # last warning (not run), 6 the stop.
    assert [e["action"] for e in events] == [
        "warn", "warn", "warn", "last_warning", "stop"]
    last = sent[-1]
    assert "tools" not in last and last["think"] is False
    assert "src/a.py" in last["messages"][-1]["content"]
    # Hard verdicts answer the call without running it.
    tool_msgs = [m for m in sent[-1]["messages"] if m.get("role") == "tool"]
    assert "This call was not run" in tool_msgs[4]["content"]
    assert len(ran) == 1      # the first call; the rest were the dedup stub


def test_run_loop_mistake_limit_after_six_failures_in_a_row():
    script = [_turn("read_file", _args(path=f"missing{k}.py"), f"c{k}")
              for k in range(6)] + [_final("Report.")]
    out, sent, events, ran = _run(script, lambda n, x: "error: path not found")
    assert out["_stop_reason"] == "mistake_limit"
    assert events[-1]["guard"] == "mistake_limit" and events[-1]["count"] == 6
    assert "6 tool calls in a row have failed" in sent[-1]["messages"][-1]["content"]


def test_run_loop_soft_warning_rides_on_the_tool_result():
    a = _args(path="src/a.py")
    script = [_turn("read_file", a, "c0"), _turn("read_file", a, "c1"),
              _final("ok")]
    out, sent, events, ran = _run(script, lambda n, x: "body")
    assert "_stop_reason" not in out
    second = [m for m in sent[-1]["messages"] if m.get("role") == "tool"][1]
    assert "[SYSTEM MESSAGE] Detected 2 consecutive identical calls" in second["content"]


def test_mistake_limit_is_configurable():
    script = [_turn("read_file", _args(path=f"m{k}.py"), f"c{k}")
              for k in range(2)] + [_final("Report.")]
    out, *_ = _run(script, lambda n, x: "error: nope", mistake_limit=2)
    assert out["_stop_reason"] == "mistake_limit"
