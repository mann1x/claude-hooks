"""Direct tests for ``claude_hooks.agent_loop.runner``.

Caliber-proxy tests in ``test_caliber_proxy.py::TestAgentLoop`` cover
the integrated path through ``server.run_agent_loop``; this file
exercises the runner in isolation with a hand-rolled chat_fn and tool
executor so future callers (advisor, others) have a clean contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from claude_hooks.agent_loop import runner


# Minimal toolspec the runner can pass through.
TOOL_SPECS = [{
    "type": "function",
    "function": {"name": "echo", "parameters": {"type": "object"}},
}]


def _stop(content: str) -> dict:
    return {"choices": [{
        "message": {"role": "assistant", "content": content},
        "finish_reason": "stop",
    }]}


def _tool(name: str, args: str = "{}", tc_id: str = "tc_1") -> dict:
    return {"choices": [{
        "message": {
            "role": "assistant",
            "tool_calls": [{
                "id": tc_id, "type": "function",
                "function": {"name": name, "arguments": args},
            }],
        },
        "finish_reason": "tool_calls",
    }]}


class TestRunLoopBasic:
    def test_no_tool_call_returns_immediately(self):
        responses = [_stop("hello")]

        def chat(p):
            return responses.pop(0)

        out = runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "should not be called",
        )
        assert out["choices"][0]["message"]["content"] == "hello"

    def test_tool_call_then_answer(self):
        responses = [
            _tool("echo", '{"x":1}'),
            _stop("done"),
        ]
        executed: list[tuple[str, str]] = []

        def chat(p):
            return responses.pop(0)

        def execute(name, args, cwd):
            executed.append((name, args))
            return "tool-output"

        out = runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=execute,
        )
        assert out["choices"][0]["message"]["content"] == "done"
        assert executed == [("echo", '{"x":1}')]
        assert responses == []

    def test_iteration_cap(self):
        # Always tool-call; loop should bail at max_iterations.
        def chat(p):
            return _tool("echo", '{"i":1}')

        out = runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False,
                                     max_iterations=3,
                                     force_answer_after=0),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "x",
        )
        assert out["choices"][0]["finish_reason"] == "tool_calls"


class TestForceAnswerAfter:
    def test_strips_tools_after_n_rounds(self):
        seen_tools: list[bool] = []

        def chat(p):
            seen_tools.append("tools" in p)
            return _tool("echo", '{"i":1}')

        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False,
                                     force_answer_after=2,
                                     max_iterations=4),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "x",
        )
        # iters 0,1 have tools; 2,3 must NOT.
        assert seen_tools == [True, True, False, False]

    def test_zero_disables_strip(self):
        seen_tools: list[bool] = []

        def chat(p):
            seen_tools.append("tools" in p)
            return _tool("echo", '{"i":1}')

        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False,
                                     force_answer_after=0,
                                     max_iterations=3),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "x",
        )
        assert all(seen_tools)


class TestForceFirstToolCall:
    def test_pins_required_until_first_tool(self):
        seen_choice: list[Any] = []
        responses = [
            _tool("echo", '{"i":1}'),
            _stop("done"),
        ]

        def chat(p):
            seen_choice.append(p.get("tool_choice"))
            return responses.pop(0)

        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=True,
                                     max_iterations=4),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "x",
        )
        # iter 0 = required (pre-tool); iter 1 = auto.
        assert seen_choice[0] == "required"
        assert seen_choice[1] == "auto"

    def test_retry_on_skipped_tool(self):
        responses = [_stop("answer without tool"), _tool("echo"), _stop("done")]
        msgs_seen: list[list[dict]] = []

        def chat(p):
            msgs_seen.append(list(p.get("messages") or []))
            return responses.pop(0)

        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=True,
                                     force_first_retry_enabled=True,
                                     max_iterations=4,
                                     force_answer_after=0),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "x",
        )
        # Second call must contain the corrective user message.
        last_user = [m for m in msgs_seen[1]
                     if m.get("role") == "user"]
        assert any("skipped tool use" in (m.get("content") or "")
                   for m in last_user)

    def test_retry_disabled(self):
        # When retry is disabled, we don't second-guess the model.
        responses = [_stop("answer without tool")]

        def chat(p):
            return responses.pop(0)

        out = runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=True,
                                     force_first_retry_enabled=False,
                                     max_iterations=4),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "x",
        )
        assert out["choices"][0]["message"]["content"] == "answer without tool"


class TestPreseed:
    def test_preseed_messages_injected(self):
        responses = [_stop("done")]
        seen_msgs: list[list[dict]] = []

        def chat(p):
            seen_msgs.append(list(p.get("messages") or []))
            return responses.pop(0)

        def preseed(cwd):
            assistant_msg = {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "p0", "type": "function",
                    "function": {"name": "survey", "arguments": "{}"},
                }],
            }
            tool_msg = {"role": "tool", "tool_call_id": "p0",
                        "name": "survey", "content": "MAP"}
            return [assistant_msg, tool_msg], "survey|{}", "MAP"

        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "should not call",
            preseed_builder=preseed,
        )
        # Messages on first chat call must include the preseed pair.
        assert any(m.get("role") == "tool" and m.get("content") == "MAP"
                   for m in seen_msgs[0])

    def test_preseed_skipped_when_tools_disabled(self):
        called = []

        def preseed(cwd):
            called.append(True)
            return None

        responses = [_stop("done")]
        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(tools_available=False),
            tool_specs=[],
            chat_fn=lambda p: responses.pop(0),
            tool_executor=lambda n, a, c: "x",
            preseed_builder=preseed,
        )
        assert called == []  # never invoked when tools off


class TestToolBurstHandling:
    def test_dedup_identical_calls_within_turn(self):
        executed: list[str] = []

        def chat(p):
            if not getattr(chat, "first", False):
                chat.first = True  # type: ignore[attr-defined]
                return {"choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "a", "type": "function",
                             "function": {"name": "echo",
                                          "arguments": '{"x":1}'}},
                            {"id": "b", "type": "function",
                             "function": {"name": "echo",
                                          "arguments": '{"x":1}'}},
                        ],
                    },
                    "finish_reason": "tool_calls",
                }]}
            return _stop("done")

        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: executed.append(a) or "X",
        )
        # Identical tool_calls collapse — executor called once only.
        assert executed == ['{"x":1}']

    def test_cap_max_tool_calls_per_turn(self):
        # Three distinct tool calls, cap at 2.
        executed: list[str] = []

        def chat(p):
            if not getattr(chat, "first", False):
                chat.first = True  # type: ignore[attr-defined]
                return {"choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "a", "type": "function",
                             "function": {"name": "echo",
                                          "arguments": '{"x":1}'}},
                            {"id": "b", "type": "function",
                             "function": {"name": "echo",
                                          "arguments": '{"x":2}'}},
                            {"id": "c", "type": "function",
                             "function": {"name": "echo",
                                          "arguments": '{"x":3}'}},
                        ],
                    },
                    "finish_reason": "tool_calls",
                }]}
            return _stop("done")

        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False,
                                     max_tool_calls_per_turn=2),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: executed.append(a) or "X",
        )
        assert executed == ['{"x":1}', '{"x":2}']


class TestMergeTools:
    def test_no_existing(self):
        out = runner.merge_tools(None, TOOL_SPECS)
        assert out == TOOL_SPECS

    def test_caller_wins_on_collision(self):
        existing = [{
            "type": "function",
            "function": {"name": "echo", "description": "caller version"},
        }]
        out = runner.merge_tools(existing, TOOL_SPECS)
        # Caller's echo stays; runner's echo dropped.
        names = [t["function"]["name"] for t in out]
        assert names.count("echo") == 1
        assert out[0]["function"].get("description") == "caller version"

    def test_unique_appended(self):
        existing = [{
            "type": "function",
            "function": {"name": "other"},
        }]
        out = runner.merge_tools(existing, TOOL_SPECS)
        names = [t["function"]["name"] for t in out]
        assert "other" in names and "echo" in names


# ----------------------- Phase 2 callbacks -------------------------- #
# `on_iter` and `on_tool` were added so consultants' MessageRecorder
# can listen to every chat call + tool execution. Both default to None
# for backward compatibility — caliber + advisor pass nothing.

class TestCallbacks:
    def test_on_iter_fires_per_chat_call(self):
        responses = [_tool("echo", '{"a":1}'), _stop("done")]
        seen: list[tuple[int, dict]] = []

        def chat(p):
            return responses.pop(0)

        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "out",
            on_iter=lambda i, req, resp, dt: seen.append((i, resp)),
        )
        assert [s[0] for s in seen] == [0, 1]
        assert seen[0][1]["choices"][0]["finish_reason"] == "tool_calls"
        assert seen[1][1]["choices"][0]["message"]["content"] == "done"

    def test_on_tool_fires_per_tool_call_with_duration(self):
        responses = [_tool("echo", '{"x":1}'), _stop("done")]
        events: list[tuple] = []

        def chat(p):
            return responses.pop(0)

        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "result-for-x",
            on_tool=lambda *a: events.append(a),
        )
        assert len(events) == 1
        name, args, output, dt_ms, err = events[0]
        assert name == "echo"
        assert args == '{"x":1}'
        assert output == "result-for-x"
        assert dt_ms >= 0
        assert err is None

    def test_on_tool_dedup_fires_with_zero_duration(self):
        # Same tool+args called twice — second call hits the dedup
        # stub. Both should fire on_tool.
        responses = [_tool("echo", '{"x":1}', tc_id="t1"),
                     _tool("echo", '{"x":1}', tc_id="t2"),
                     _stop("done")]
        events: list[tuple] = []

        def chat(p):
            return responses.pop(0)

        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "real",
            on_tool=lambda *a: events.append(a),
        )
        assert len(events) == 2
        # First fires with real output + nonzero (or zero on fast clocks) ms
        assert events[0][2] == "real"
        # Second is the dedup stub: dt_ms == 0, output starts with "(duplicate"
        assert events[1][3] == 0
        assert events[1][2].startswith("(duplicate")

    def test_callback_exceptions_swallowed(self):
        # A misbehaving callback must NOT crash the loop. Caliber +
        # advisor would otherwise see hard failures from a downstream
        # observability bug.
        responses = [_stop("ok")]

        def chat(p):
            return responses.pop(0)

        def bad_iter(*a):
            raise RuntimeError("recorder offline")

        out = runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            cwd="/tmp",
            config=runner.LoopConfig(force_first_tool_call=False),
            tool_specs=TOOL_SPECS,
            chat_fn=chat,
            tool_executor=lambda n, a, c: "",
            on_iter=bad_iter,
        )
        assert out["choices"][0]["message"]["content"] == "ok"

    def test_on_tool_records_executor_failure_and_reraises(self):
        responses = [_tool("echo", '{"x":1}')]
        events: list[tuple] = []

        def chat(p):
            return responses.pop(0)

        def bad_executor(name, args, cwd):
            raise RuntimeError("network error")

        with pytest.raises(RuntimeError, match="network error"):
            runner.run_loop(
                {"model": "m", "messages": [{"role": "user", "content": "go"}]},
                cwd="/tmp",
                config=runner.LoopConfig(force_first_tool_call=False),
                tool_specs=TOOL_SPECS,
                chat_fn=chat,
                tool_executor=bad_executor,
                on_tool=lambda *a: events.append(a),
            )
        assert len(events) == 1
        # error column populated, output empty
        assert "network error" in events[0][4]
        assert events[0][2] == ""
