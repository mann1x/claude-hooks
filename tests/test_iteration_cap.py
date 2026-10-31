"""Plan D6: a warning a margin before the iteration cap, and one tool-less
report at it (Cerebriline ``agent-iteration-cap.ts``)."""
from __future__ import annotations

from claude_hooks.agent_loop import runner

SPEC = [{"type": "function", "function": {"name": "read_file",
                                          "parameters": {"type": "object"}}}]


def _tool(i):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {
            "name": "read_file", "arguments": f'{{"path": "f{i}.py"}}'}}]},
        "finish_reason": "tool_calls"}]}


def _answer(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def _run(cap, answer_at=None):
    sent, events = [], []

    def chat_fn(payload):
        sent.append(dict(payload, messages=[dict(m) for m in payload["messages"]]))
        n = len(sent) - 1
        if "tools" not in payload:
            return _answer("Report.")
        return _answer("Done.") if n == answer_at else _tool(n)

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False, max_iterations=cap,
                                 force_answer_after=0, on_guard=events.append),
        tool_specs=SPEC, chat_fn=chat_fn, tool_executor=lambda n, a, c: "body")
    return out, sent, [e for e in events if e["guard"] == "iteration_cap"]


def test_margin_matches_the_fork():
    assert [runner.cap_warning_margin(c) for c in (3, 6, 10, 15, 25, 48, 200)] \
        == [1, 1, 1, 2, 3, 5, 5]


def test_warned_once_then_reported_at_the_cap():
    out, sent, events = _run(8)
    assert [e["action"] for e in events] == ["warn", "stop"]
    # Margin 1 at a cap of 8: the warning rides on turn 8's input.
    warned = [i for i, p in enumerate(sent)
              if any("This is turn" in (m.get("content") or "")
                     for m in p["messages"])]
    assert warned[0] == 7
    assert sent[7]["messages"][-1]["role"] == "tool"
    assert "turn 8 of the 8" in sent[7]["messages"][-1]["content"]
    final = sent[-1]
    assert "tools" not in final and final["think"] is False
    assert final["messages"][-1]["content"] == runner.CAP_FINAL_REPORT
    assert out["choices"][0]["message"]["content"] == "Report."
    assert out["_stop_reason"] == out["_failure_class"] == "iteration_cap"


def test_wide_margin_warns_earlier():
    _, sent, events = _run(25)
    assert events[0]["iteration"] == 22   # turn 23: 2 left < margin 3


def test_an_answer_before_the_cap_is_left_alone():
    out, sent, events = _run(8, answer_at=3)
    assert events == []
    assert out["choices"][0]["message"]["content"] == "Done."
    assert "_stop_reason" not in out


def test_answer_on_the_last_turn_needs_no_report():
    out, sent, events = _run(8, answer_at=7)
    assert [e["action"] for e in events] == ["warn"]
    assert len(sent) == 8 and out["choices"][0]["message"]["content"] == "Done."
