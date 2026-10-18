"""A lane that wrote tests that never ran green is asked once to run them
before it reports (council run 3, mail #1633)."""
from __future__ import annotations

import json

from claude_hooks import test_reruns as tr
from claude_hooks.agent_loop import runner


def _call(cid, name, **args):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _conv(*steps):
    """steps: (call, result) pairs, one assistant turn each."""
    msgs = [{"role": "user", "content": "q"}]
    for call, result in steps:
        msgs.append({"role": "assistant", "content": "", "tool_calls": [call]})
        msgs.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    return msgs


OK = "exit=0 wall=1s killed_by=none\n--- stdout (20 chars) ---\n Tests  3 passed (3)"
FAIL = "exit=1 wall=1s killed_by=none\n--- stdout (30 chars) ---\nCannot find module"


class TestClassify:

    def test_written_and_never_run(self):
        msgs = _conv((_call("a", "write_file", path="src/x.test.ts", content="t"),
                      "wrote src/x.test.ts"))
        assert tr.unrun_tests(msgs) == {"src/x.test.ts": "never run after it was written"}

    def test_heredoc_run_in_the_same_command_that_fails(self):
        msgs = _conv((_call("a", "run_command", command=(
            "cat > src/g.test.ts <<'EOF'\nx\nEOF\nbun x vitest run src/g.test.ts")), FAIL))
        assert tr.unrun_tests(msgs) == {"src/g.test.ts": "its last run failed (exit 1)"}

    def test_fixed_and_rerun_green_is_fine(self):
        msgs = _conv(
            (_call("a", "write_file", path="src/x.test.ts", content="t"), "wrote"),
            (_call("b", "run_command", command="bun x vitest run src/x.test.ts"), FAIL),
            (_call("c", "write_file", path="src/x.test.ts", content="t2"), "wrote"),
            (_call("d", "run_command", command="cd core && bun x vitest run x.test.ts"), OK))
        assert tr.unrun_tests(msgs) == {}

    def test_a_rewrite_after_a_green_run_needs_another_run(self):
        msgs = _conv(
            (_call("a", "write_file", path="t/test_a.py", content="t"), "wrote"),
            (_call("b", "run_command", command="pytest t/test_a.py"), OK),
            (_call("c", "write_file", path="t/test_a.py", content="t2"), "wrote"))
        assert tr.unrun_tests(msgs) == {"t/test_a.py": "never run after it was written"}

    def test_not_tests_and_not_test_runs(self):
        msgs = _conv(
            (_call("a", "write_file", path="src/util.ts", content="t"), "wrote"),
            (_call("b", "write_file", path="src/x.test.ts", content="t"), "wrote"),
            (_call("c", "run_command", command="cat src/x.test.ts"), OK))
        assert tr.unrun_tests(msgs) == {"src/x.test.ts": "never run after it was written"}
        assert not tr.TEST_LIKE.search("cat vitest.config.ts")
        # a sibling whose name contains this one's is not its run
        msgs = _conv(
            (_call("a", "write_file", path="src/a.test.ts", content="t"), "wrote"),
            (_call("b", "run_command", command="bun x vitest run src/aa.test.ts"), OK))
        assert tr.unrun_tests(msgs) == {"src/a.test.ts": "never run after it was written"}


SPEC = [{"type": "function", "function": {"name": n, "parameters": {"type": "object"}}}
        for n in ("write_file", "run_command")]


def _turn(content="", calls=()):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = list(calls)
    return {"choices": [{"message": msg, "finish_reason": "tool_calls" if calls else "stop",
                         "done_reason": "stop"}]}


def _run(script, specs=SPEC, results=None):
    sent, events = [], []

    def chat_fn(payload):
        sent.append(dict(payload, messages=list(payload["messages"])))
        return script.pop(0)

    results = dict(results or {})

    def execute(name, args, cwd):
        return results.get(name, "wrote")

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False, force_answer_after=0,
                                 on_guard=events.append),
        tool_specs=specs, chat_fn=chat_fn, tool_executor=execute)
    return out, sent, events


class TestTheLoopNudges:

    def test_once_then_the_report_stands(self):
        out, sent, events = _run([
            _turn(calls=[_call("a", "write_file", path="src/x.test.ts", content="t")]),
            _turn("REPORT one"),
            _turn("REPORT two"),          # still not run: the nudge is spent
        ])
        assert len(sent) == 3
        note = sent[2]["messages"][-1]
        assert note.get("_harness") and "`src/x.test.ts`: never run" in note["content"]
        assert [e for e in events if e.get("guard") == "unrun_tests"][0]["files"] == [
            "src/x.test.ts"]
        assert sent[2]["messages"][-2] == {"role": "assistant", "content": "REPORT one"}

    def test_a_lane_that_ran_its_test_green_is_not_asked(self):
        out, sent, events = _run([
            _turn(calls=[_call("a", "write_file", path="src/x.test.ts", content="t")]),
            _turn(calls=[_call("b", "run_command", command="bun x vitest run src/x.test.ts")]),
            _turn("REPORT"),
        ], results={"run_command": OK})
        assert len(sent) == 3
        assert not [e for e in events if e.get("guard") == "unrun_tests"]

    def test_no_shell_no_nudge(self):
        specs = [SPEC[0]]
        out, sent, events = _run([
            _turn(calls=[_call("a", "write_file", path="src/x.test.ts", content="t")]),
            _turn("REPORT"),
        ], specs=specs)
        assert len(sent) == 2
