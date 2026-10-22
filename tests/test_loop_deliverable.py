"""``LoopConfig.deliverable``: the messages that end a loop ask for the
role's own answer, not a findings report (2026-10-22: capped planners,
critics and synthesizers delivered "what I found, what is still open" in
place of their plan, verdict or answer)."""
from __future__ import annotations

from claude_hooks.agent_loop import runner
from consultants.engine import council

SPEC = [{"type": "function", "function": {"name": "read_file",
                                          "parameters": {"type": "object"}}}]
PLAN = "your plan: the numbered steps"


def _tool(i):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {
            "name": "read_file", "arguments": f'{{"path": "f{i}.py"}}'}}]},
        "finish_reason": "tool_calls"}]}


def _answer(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def _run(deliverable="", **cfg):
    sent = []

    def chat_fn(payload):
        sent.append(dict(payload, messages=[dict(m) for m in payload["messages"]]))
        # The forced report is the one tool-less call that ends on a harness
        # note; every other turn calls a tool, offered or not.
        forced = ("tools" not in payload
                  and payload["messages"][-1].get("_harness"))
        return _answer("Answer.") if forced else _tool(len(sent))

    config = runner.LoopConfig(force_first_tool_call=False, deliverable=deliverable,
                               **{"max_iterations": 4, "force_answer_after": 0, **cfg})
    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=config, tool_specs=SPEC, chat_fn=chat_fn,
        tool_executor=lambda n, a, c: "body")
    return out, sent


def _texts(sent):
    return [m.get("content") or "" for p in sent for m in p["messages"]]


def test_the_cap_asks_for_the_deliverable():
    out, sent = _run(PLAN)
    assert sent[-1]["messages"][-1]["content"] == \
        runner.CAP_FINAL_DELIVERABLE.format(deliverable=PLAN)
    assert any(runner.CAP_WARNING_DELIVERABLE.format(
        turn=4, cap=4, deliverable=PLAN) in t for t in _texts(sent))
    assert not any("what you found" in t for t in _texts(sent))
    assert out["_stop_reason"] == "iteration_cap"


def test_tools_off_asks_for_the_deliverable():
    # Tools stripped after turn 1; the model calls twice more → the second
    # refusal ends the loop.
    out, sent = _run(PLAN, max_iterations=8, force_answer_after=1)
    answers = [m["content"] for p in sent for m in p["messages"]
               if m.get("role") == "tool" and m["content"] != "body"]
    assert answers and set(answers) == {
        runner.TOOLS_OFF_ANSWER_DELIVERABLE.format(deliverable=PLAN)}
    assert sent[-1]["messages"][-1]["content"] == \
        runner.TOOLS_OFF_FINAL_DELIVERABLE.format(deliverable=PLAN)
    assert out["_stop_reason"] == "tools_off"


def test_wrap_up_asks_for_the_deliverable():
    out, sent = _run(PLAN, wrap_up_check=lambda: True)
    assert sent[-1]["messages"][-1]["content"] == \
        runner.WRAP_UP_FINAL_DELIVERABLE.format(deliverable=PLAN)
    assert out["_stop_reason"] == "wrap_up"


def test_no_deliverable_keeps_the_findings_report():
    _, sent = _run()
    assert sent[-1]["messages"][-1]["content"] == runner.CAP_FINAL_REPORT
    _, sent = _run(max_iterations=8, force_answer_after=1)
    assert sent[-1]["messages"][-1]["content"] == runner.TOOLS_OFF_FINAL


def test_each_tooled_role_names_its_deliverable(monkeypatch):
    seen = {}

    def loop_runner(payload, cwd, *, config, **kw):
        seen[config_role[0]] = config.deliverable
        return _answer("x")

    class Client:
        def chat(self, payload):  # pragma: no cover — loop_runner is faked
            return _answer("x")

    config_role = [None]
    for role in ("planner", "critic", "meta_critic", "synthesizer",
                 "adversary", "critic:ruling", "researcher:cross_exam"):
        config_role[0] = role
        council._role_turn(Client(), "m", [{"role": "user", "content": "q"}],
                           role=role, tool_specs=SPEC,
                           tool_executor=lambda *a: "", cwd="/tmp",
                           loop_runner=loop_runner)
    assert seen["planner"] == council.ROLE_DELIVERABLES["planner"]
    assert "DECISION:" in seen["critic"] and "DECISION:" in seen["meta_critic"]
    assert "final answer" in seen["synthesizer"]
    assert "REFUTATION:" in seen["adversary"]
    assert seen["critic:ruling"].startswith("your rulings")
    assert seen["researcher:cross_exam"] == ""     # a lane owes a report
