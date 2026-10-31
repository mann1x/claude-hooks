"""Fixes found by the first full council on opencoti (2026-10-14)."""
from __future__ import annotations

import pytest

from claude_hooks.get_advice import chat_client as cc
from consultants.engine import state_v2


def test_leading_system_turns_are_joined_into_one():
    msgs = [{"role": "system", "content": "ADDENDUM"},
            {"role": "system", "content": "MAP"},
            {"role": "system", "content": "RESEARCHER"},
            {"role": "user", "content": "Q"}]
    assert cc.single_system_messages(msgs) == [
        {"role": "system", "content": "ADDENDUM\n\nMAP\n\nRESEARCHER"},
        {"role": "user", "content": "Q"}]


def test_a_later_system_turn_becomes_a_note_and_roles_alternate():
    msgs = [{"role": "system", "content": "S"},
            {"role": "user", "content": "Q"},
            {"role": "system", "content": "[3 messages elided]"},
            {"role": "assistant", "content": "A"},
            {"role": "system", "content": "marker"},
            {"role": "user", "content": "go on"}]
    out = cc.single_system_messages(msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
    assert out[1]["content"] == "Q\n\n[system note]\n[3 messages elided]"
    assert out[3]["content"] == "[system note]\nmarker\n\ngo on"


def test_tool_turns_and_calls_pass_through():
    msgs = [{"role": "system", "content": "S"},
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "R", "tool_call_id": "1"}]
    assert cc.single_system_messages(msgs) == msgs


def test_opencoti_body_has_one_system_turn():
    c = cc.OpenAICompatChatClient("http://oc:8240")
    body = c._prepare({"model": "m", "messages": [
        {"role": "system", "content": "a"}, {"role": "system", "content": "b"},
        {"role": "user", "content": "q"}]}, strip_think=False, stream=False)
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_error_reducers():
    assert state_v2.join_errors(None, "a") == "a"
    assert state_v2.join_errors("a", None) == "a"
    assert state_v2.join_errors("a", "b") == "a | b"
    assert state_v2.join_errors("a | b", "b") == "a | b"
    assert state_v2.latest_non_none("researcher", None) == "researcher"
    assert state_v2.latest_non_none(None, "critic") == "critic"


def test_two_lanes_failing_in_one_step_do_not_abort_the_run():
    pytest.importorskip("langgraph.graph")
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Send
    from consultants.engine.graph import CouncilState

    def fan(state):
        return [Send("lane", {"plan_item": str(i)}) for i in range(3)]

    def lane(state):
        return {"error": f"researcher failed: lane {state['plan_item']}",
                "_role_failed": "researcher",
                "research": [f"(lane {state['plan_item']} failed)"]}

    g = StateGraph(CouncilState)
    g.add_node("lane", lane)
    g.add_conditional_edges(START, fan, ["lane"])
    g.add_edge("lane", END)
    out = g.compile().invoke({"question": "q", "research": []})
    assert out["_role_failed"] == "researcher"
    assert sorted(out["error"].split(" | ")) == [
        f"researcher failed: lane {i}" for i in range(3)]
    assert len(out["research"]) == 3


def test_every_send_hands_the_lane_its_run_record():
    """A Send lane sees only the keys in its own dict. Without
    ``runtime_control`` a fanned-out lane ran with no stall monitor, no
    typed protocol and no budget / think / sampling settings (found by
    the first opencoti council, 2026-10-14)."""
    import ast
    import inspect
    from consultants.engine import graph
    tree = ast.parse(inspect.getsource(graph))
    sends = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Send"]
    assert sends, "no Send found"
    for call in sends:
        payload = call.args[1]
        assert isinstance(payload, ast.Dict)
        keys = {k.value for k in payload.keys if isinstance(k, ast.Constant)}
        assert "runtime_control" in keys, f"Send at line {call.lineno}"
