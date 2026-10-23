"""Calls the loop makes outside its own turn — an answer continuation and a
forced report — go through ``LoopConfig.prepare_payload`` like every other
call (bug-1101 / bug-1102), and a second continuation sends the partial
once, not twice."""
from __future__ import annotations

from claude_hooks.agent_loop import runner

SPEC = [{"type": "function", "function": {"name": "read_file",
                                          "parameters": {"type": "object"}}}]


def _cut(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "length"}]}


def _done(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def _tool(i):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {
            "name": "read_file", "arguments": f'{{"path": "f{i}.py"}}'}}]},
        "finish_reason": "tool_calls"}]}


def _loop(chat_fn, prepared, **cfg):
    def prepare(payload):
        prepared.append(len(payload["messages"]))
        payload.setdefault("options", {})["num_predict"] = 1234

    config = runner.LoopConfig(force_first_tool_call=False,
                               prepare_payload=prepare,
                               **{"max_iterations": 4, "force_answer_after": 0,
                                  **cfg})
    return runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=config, tool_specs=SPEC, chat_fn=chat_fn,
        tool_executor=lambda n, a, c: "body")


def test_continuations_are_prepared_and_carry_the_partial_once():
    sent, prepared = [], []
    replies = iter([_cut("A" * 50), _cut("B" * 50), _done("C")])

    def chat_fn(payload):
        sent.append(payload)
        return next(replies)

    out = _loop(chat_fn, prepared, max_answer_continuations=3)
    assert out["choices"][0]["message"]["content"] == "A" * 50 + "B" * 50 + "C"
    assert len(sent) == 3 and len(prepared) == 3      # every call prepared
    assert all(p["options"]["num_predict"] == 1234 for p in sent)
    second = [m.get("content") or "" for m in sent[2]["messages"]]
    assert "".join(second).count("A" * 50) == 1        # not sent twice
    assert second[-2] == "A" * 50 + "B" * 50


def test_a_continuation_keeps_what_the_budgeter_compacted():
    sent = []
    replies = iter([_cut("A"), _cut("B"), _done("C")])

    seen = []

    def compact(payload):
        msgs = payload["messages"]
        seen.append([m.get("content") for m in msgs])
        payload["messages"] = [{"role": "user", "content": "digest"}] + msgs[-2:]

    def chat_fn(payload):
        sent.append([m.get("content") for m in payload["messages"]])
        return next(replies)

    runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False,
                                 prepare_payload=compact,
                                 max_answer_continuations=3),
        tool_specs=SPEC, chat_fn=chat_fn, tool_executor=lambda n, a, c: "")
    assert sent[2][0] == "digest" and "q" not in sent[2]
    # The second continuation starts from the compacted history, so the
    # budgeter is not handed the whole original again.
    assert "q" not in seen[2]


def test_without_a_budgeter_the_partial_is_still_sent_once():
    sent = []
    replies = iter([_cut("A" * 50), _cut("B" * 50), _done("C")])

    def chat_fn(payload):
        sent.append(payload)
        return next(replies)

    runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False,
                                 max_answer_continuations=3),
        tool_specs=SPEC, chat_fn=chat_fn, tool_executor=lambda n, a, c: "")
    text = "".join(m.get("content") or "" for m in sent[2]["messages"])
    assert text.count("A" * 50) == 1


def test_the_forced_report_is_prepared():
    sent, prepared = [], []

    def chat_fn(payload):
        sent.append(payload)
        if "tools" not in payload and payload["messages"][-1].get("_harness"):
            return _done("Report.")
        return _tool(len(sent))

    out = _loop(chat_fn, prepared)
    assert out["_stop_reason"] == "iteration_cap"
    assert len(prepared) == len(sent)                  # forced call included
    assert sent[-1]["options"]["num_predict"] == 1234
    assert sent[-1]["think"] is False                  # still think-off


def test_a_raising_budgeter_does_not_stop_the_forced_report():
    def chat_fn(payload):
        if "tools" not in payload and payload["messages"][-1].get("_harness"):
            return _done("Report.")
        return _tool(1)

    def boom(payload):
        raise RuntimeError("x")

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False, prepare_payload=boom,
                                 max_iterations=3, force_answer_after=0),
        tool_specs=SPEC, chat_fn=chat_fn, tool_executor=lambda n, a, c: "")
    assert out["choices"][0]["message"]["content"] == "Report."
