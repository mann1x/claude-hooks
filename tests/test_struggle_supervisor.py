"""Plan D2: the struggle supervisor — nudge once, stop on the budget loop."""
from __future__ import annotations

from claude_hooks import struggle_supervisor as ss
from claude_hooks.agent_loop import runner

BUDGET_MSG = "I have used my thinking budget."


def test_nudge_after_is_half_the_cap_floored():
    assert ss.nudge_after(25) == 12
    assert ss.nudge_after(6) == 8          # a short loop is never nudged for length
    assert ss.nudge_after(None) == 20


def test_budget_loop_nudges_then_stops_after_grace():
    s = ss.StruggleSupervisor(max_iterations=40)
    moves = [s.close_iteration(i, budget_spent=i in {1, 3, 5, 9, 10})
             for i in range(11)]
    assert moves[5] == ("nudge", "thinking-budget")      # 3 of the last 6
    # Grace: iterations 6-7 never count; 9 and 10 are the two after.
    assert moves[10] == ("stop", "thinking-budget")
    assert [m for m in moves if m] == [("nudge", "thinking-budget"),
                                       ("stop", "thinking-budget")]
    assert s.spent_after_nudge() == 2


def test_long_and_still_finding_things_is_never_nudged():
    """Guard replay: 204 of 443 count-only length nudges landed on lanes
    whose next calls were mostly new."""
    s = ss.StruggleSupervisor(max_iterations=20)
    moves = [s.close_iteration(i, budget_spent=False,
                               calls=[(f"read_file:{i}", f"body {i}", False)])
             for i in range(20)]
    assert [m for m in moves if m] == []


def test_long_and_stale_is_nudged_once_and_never_stopped():
    # The length nudge never becomes a stop without the budget loop.
    s = ss.StruggleSupervisor(max_iterations=20)
    moves = [s.close_iteration(i, budget_spent=False,
                               calls=[("read_file:same", "same body", False)])
             for i in range(20)]
    assert [m for m in moves if m] == [("nudge", "non-progress")]
    assert moves[9] == ("nudge", "non-progress")


def test_a_failing_call_is_never_new():
    s = ss.StruggleSupervisor(max_iterations=20)
    for i in range(6):
        s.close_iteration(i, budget_spent=False,
                          calls=[(f"read_file:{i}", f"error: no file {i}", True)])
    assert s.stale()


def test_failing_calls_nudge_only_after_the_floor():
    s = ss.StruggleSupervisor(max_iterations=40)
    assert s.close_iteration(2, budget_spent=False,
                             failures=[True, True, True]) is None
    assert s.close_iteration(8, budget_spent=False,
                             failures=[True]) == ("nudge", "struggle")


# ---- run_loop ------------------------------------------------------- #

def _turn(i, spent):
    msg = {"role": "assistant", "content": "",
           "tool_calls": [{"id": f"c{i}", "type": "function", "function": {
               "name": "read_file", "arguments": f'{{"path": "f{i}.py"}}'}}]}
    if spent:
        msg["thinking"] = "Let me probe once more. " * 20 + BUDGET_MSG
    return {"choices": [{"message": msg, "finish_reason": "tool_calls"}]}


def _run(spent_iters, n, **cfg):
    script = [_turn(i, i in spent_iters) for i in range(n)] + [
        {"choices": [{"message": {"role": "assistant", "content": "Summary."},
                      "finish_reason": "stop"}]}]
    sent, events = [], []

    def chat_fn(payload):
        msgs = payload.get("messages") or []
        if msgs and msgs[0].get("role") == "system" and "condens" in (msgs[0].get("content") or "").lower():
            return {"choices": [{"message": {"role": "assistant", "content": "note"},
                                 "finish_reason": "stop"}]}
        sent.append(dict(payload, messages=[dict(m) for m in msgs]))
        return script.pop(0)

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False, max_iterations=40,
                                 force_answer_after=0, on_guard=events.append,
                                 thinking_budget_message=BUDGET_MSG,
                                 capped_thinking_enabled=False, **cfg),
        tool_specs=[], chat_fn=chat_fn,
        tool_executor=lambda name, args, cwd: f"body of {args}")
    return out, sent, events


def test_run_loop_budget_loop_ends_with_a_struggle_report():
    out, sent, events = _run({1, 3, 5, 9, 10}, 11)
    assert out["_stop_reason"] == "struggle"
    assert out["choices"][0]["message"]["content"] == "Summary."
    assert [(e["action"], e["reason"]) for e in events
            if e["guard"] == "struggle"] == [("nudge", "thinking-budget"),
                                             ("stop", "thinking-budget")]
    # The nudge rode on the tool result the model read next.
    tool6 = [m for m in sent[6]["messages"] if m.get("role") == "tool"][-1]
    assert "Commit what you have now" in tool6["content"]
    assert "tools" not in sent[-1] and sent[-1]["think"] is False


def test_run_loop_a_heeded_nudge_is_never_a_stop():
    out, sent, events = _run({1, 3, 5}, 11)
    assert "_stop_reason" not in out
    assert out["choices"][0]["message"]["content"] == "Summary."
