"""Plan D3 (standard): a turn without a tool call is not always an answer."""
from __future__ import annotations

from claude_hooks import silent_turns as st
from claude_hooks.agent_loop import runner

UNPARSED = ("<tool_call>\n<function=read_file>\n<parameter=path>\nsrc/a.py\n"
            "</parameter>\n</function>\n</tool_call>")
RESTARTED = ("<tool_call>\n<function=edit_file>\n<parameter=body>\nx = 1\n"
             "<tool_call>\n<function=edit_file>\n<parameter=body>\nx = 1\n"
             "</parameter>\n</function>\n</tool_call>")


def test_detectors():
    assert st.thinking_only("", "thinking...") is False
    assert st.thinking_only("", "I will call <function=grep> now") is True
    assert st.thinking_only("answer", "thinking") is None
    assert st.thinking_only("", "") is None
    u = st.unparsed_tool_call(UNPARSED)
    assert u.name == "read_file" and u.restarts == 0
    assert st.unparsed_tool_call(RESTARTED).restarts == 1
    assert st.unparsed_tool_call("Calls go in <tool_call> tags.") is None
    assert st.announced_intent("Found the bug. Next, I'll read src/b.py.")
    assert st.announced_intent("Report done. Let me know if you need more.") is None
    assert st.announced_intent("I'll check it. The task is complete.") is None
    # Guard replay (2026-10-16): two real reports ended this way.
    assert st.announced_intent("It logs the cut and lets Claude Code re-issue.") is None
    assert st.announced_intent("Opening the matrix would let me cite cell counts.") is None
    assert st.announced_intent("Found it. Let\u2019s read the caller next.")
    assert st.announced_intent("Found it. Lets check the caller next.")


#: minimax-m3:cloud hands its tool calls back as text in this shape.
MINIMAX = ("Need the caller.]<]minimax[>[<tool_call>\n]<]minimax[>[<invoke name=\"grep\">"
           "]<]minimax[>[<path>src]<]minimax[>[</path>]<]minimax[>[<pattern>def run"
           "]<]minimax[>[</pattern>]<]minimax[>[</invoke>\n]<]minimax[>[<invoke name=\"read_file\">"
           "]<]minimax[>[<path>src/a.py]<]minimax[>[</path>]<]minimax[>[</invoke>\n"
           "]<]minimax[>[</tool_call>")


def test_minimax_invoke_markup_is_an_unparsed_call():
    u = st.unparsed_tool_call(MINIMAX)
    assert u is not None and u.name == "read_file" and u.restarts == 0
    assert st.thinking_only("", MINIMAX) is True
    assert st.unparsed_tool_call('Write <invoke name="grep"> to search.') is None


def _say(content="", thinking="", tool=False, cid="c"):
    msg = {"role": "assistant", "content": content}
    if thinking:
        msg["thinking"] = thinking
    if tool:
        msg["tool_calls"] = [{"id": cid, "type": "function", "function": {
            "name": "read_file", "arguments": f'{{"path": "{cid}.py"}}'}}]
    return {"choices": [{"message": msg,
                         "finish_reason": "tool_calls" if tool else "stop",
                         "done_reason": "stop"}]}


SPEC = [{"type": "function", "function": {"name": "read_file",
                                          "parameters": {"type": "object"}}}]


def _run(script, **cfg):
    sent, events = [], []

    def chat_fn(payload):
        sent.append(dict(payload, messages=list(payload["messages"])))
        return script.pop(0)

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False,
                                 force_answer_after=0, on_guard=events.append,
                                 **cfg),
        tool_specs=SPEC, chat_fn=chat_fn,
        tool_executor=lambda n, a, c: "body")
    return out, sent, events


def test_thinking_only_is_reasked_twice_then_stands():
    out, sent, events = _run([_say(thinking="hm"), _say(thinking="hm"),
                              _say(thinking="hm")])
    assert len(sent) == 3
    assert [e["kind"] for e in events] == ["thinking_only", "thinking_only"]
    assert sent[1]["messages"][-1]["content"] == st.THINKING_ONLY_REMINDER


def test_a_tool_call_resets_the_thinking_budget():
    out, sent, events = _run([
        _say(thinking="hm"), _say(thinking="hm"), _say(tool=True),
        _say(thinking="hm"), _say(content="Answer.")])
    assert out["choices"][0]["message"]["content"] == "Answer."
    assert len([e for e in events if e["kind"] == "thinking_only"]) == 3


def test_call_inside_thinking_gets_its_own_reminder():
    out, sent, events = _run([_say(thinking="<function=read_file> path"),
                              _say(content="ok")])
    assert events[0]["kind"] == "tool_call_in_thinking"
    assert "ended inside your thinking" in sent[1]["messages"][-1]["content"]


def test_unparsed_call_is_reasked_with_the_attempt_kept():
    out, sent, events = _run([_say(content=UNPARSED), _say(tool=True),
                              _say(content="Answer.")])
    assert events[0]["kind"] == "unparsed_tool_call"
    hist = sent[1]["messages"]
    assert hist[-2] == {"role": "assistant", "content": UNPARSED}
    assert "`read_file`" in hist[-1]["content"]


def test_announced_intent_is_nudged_once_per_lane():
    promise = "Found the cause. Next, I'll read the caller to confirm."
    out, sent, events = _run([_say(content=promise), _say(content=promise)])
    assert [e["kind"] for e in events] == ["announced_intent"]
    assert out["choices"][0]["message"]["content"] == promise
    assert len(sent) == 2


def test_a_plain_final_report_is_never_reasked():
    out, sent, events = _run([_say(content="## Findings\n- a.py:3 is wrong.")])
    assert events == [] and len(sent) == 1


def test_no_reask_without_tools_for_intent_or_unparsed():
    out, sent, events = _run([_say(content=UNPARSED)], tools_available=False)
    assert events == [] and len(sent) == 1


def test_non_convergence_line_after_two_long_silent_turns():
    long = "Considering the options at length. " * 80
    out, sent, events = _run([_say(thinking=long), _say(thinking=long),
                              _say(content="Answer.")])
    first = sent[1]["messages"][-1]["content"]
    second = sent[2]["messages"][-1]["content"]
    assert "without calling a single tool" not in first
    assert "2 turns in a row without calling a single tool" in second


def test_a_truncated_answer_goes_to_continuation_not_reask():
    cut = _say(content="The answer is part")
    cut["choices"][0]["done_reason"] = "length"
    cut["choices"][0]["finish_reason"] = "length"
    out, sent, events = _run([cut, _say(content="ial and complete.")],
                             max_answer_continuations=1)
    assert events == []
    assert out["choices"][0]["message"]["content"] == \
        "The answer is partial and complete."


# ---- csl-2026-10-18-1033-5a67: a call written as text after the tools went
#: Lane 2's last turn, verbatim: its next read_file, written as text after
#: force-answer had stripped the tools. It was the lane's whole "report".
LANE2 = ("]<]minimax[>[<tool_call>\n]<]minimax[>[<invoke name=\"read_file\">]<]minimax[>["
         "<path>sdk/packages/core/src/runtime/atomic/base-revision-reads.ts]<]minimax[>["
         "</path>]<]minimax[>[</invoke>\n]<]minimax[>[</tool_call>")


def test_only_tool_call():
    assert st.only_tool_call(LANE2)
    assert st.only_tool_call(UNPARSED)
    assert st.only_tool_call(MINIMAX)           # 16 chars of prose: still no report
    report = ("## Findings\n- `src/a.py:12` drops the receipt on restore, so the "
              "next read reports an outside write.\n" + UNPARSED)
    assert not st.only_tool_call(report)
    assert not st.only_tool_call("A plain report with no calls in it at all.")
    assert not st.only_tool_call("")


def _run_stripping(script, after=1):
    sent, events = [], []

    def chat_fn(payload):
        sent.append(dict(payload, messages=list(payload["messages"])))
        return script.pop(0)

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False,
                                 force_answer_after=after, on_guard=events.append),
        tool_specs=SPEC, chat_fn=chat_fn,
        tool_executor=lambda n, a, c: "body")
    return out, sent, events


def test_a_call_written_after_the_tools_went_is_reasked_for_a_report():
    out, sent, events = _run_stripping([_say(tool=True), _say(content=LANE2),
                                        _say(content="Report: a.py:1 is fine.")])
    assert "tools" not in sent[1] and "tools" not in sent[2]
    assert [e["kind"] for e in events if e.get("guard") == "silent_turn"] == \
        ["tool_call_without_tools"]
    assert sent[2]["messages"][-1]["content"] == st.NO_TOOLS_REPORT_REMINDER
    assert out["choices"][0]["message"]["content"] == "Report: a.py:1 is fine."
    assert "_stop_reason" not in out


def test_a_lane_that_never_reports_ends_as_no_report():
    """Two of eight lanes ended "completed" on markup; a quarter of the
    audit was silently not done."""
    from consultants.engine.events import loop_stop
    out, sent, events = _run_stripping([_say(tool=True), _say(content=LANE2),
                                        _say(content=LANE2), _say(content=LANE2)])
    assert len(sent) == 4                       # two re-asks, then it stands
    assert loop_stop(out) == ("no_report", "tool_call_unparsable")
