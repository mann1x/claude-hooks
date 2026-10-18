"""Plan council-memory M4: on a shell run the critic and meta-critic can
run commands, each in a view of its own, with a lane's loop bonus."""
from __future__ import annotations

import json

from consultants.engine import council, evidence, graph
from consultants.engine.graph import GraphDeps, _tools_for

SPECS = [{"type": "function", "function": {"name": n, "parameters": {"type": "object"}}}
         for n in ("read_file", "run_command")]


class _LaneExec:
    accepts_lane = True

    def __init__(self):
        self.calls = []

    def __call__(self, name, raw, cwd, **kw):
        self.calls.append((name, kw.get("lane")))
        return "exit=0 killed_by=none\nok"


def _deps(specs=SPECS, tooled=()):
    return GraphDeps(chat_clients={}, models={}, enabled_roles=(), cwd="/x",
                     tool_specs=list(specs), tooled_roles=tooled,
                     tool_executor=_LaneExec())


class TestWhoGetsTheShell:

    def test_a_judge_with_a_lane_gets_it_without_all_roles(self):
        deps = _deps()
        kw = _tools_for(deps, "critic", "critic:2")
        assert [s["function"]["name"] for s in kw["tool_specs"]] == [
            "read_file", "run_command"]
        kw["tool_executor"]("run_command", "{}", "/x")
        assert deps.tool_executor.calls == [("run_command", "critic:2")]

    def test_the_planner_and_synthesizer_stay_without(self):
        deps = _deps(tooled=("synthesizer", "planner"))
        for role in ("synthesizer", "planner"):
            kw = _tools_for(deps, role, f"{role}:0")
            assert [s["function"]["name"] for s in kw["tool_specs"]] == ["read_file"]
        assert graph.SHELL_ROLES == {"researcher", "tool_executor", "coder",
                                     "critic", "meta_critic"}

    def test_no_shell_run_changes_nothing(self):
        assert _tools_for(_deps(SPECS[:1]), "critic", "critic:0") == {}

    def test_the_wrappers_bind_each_judge_its_own_lane(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(council, "critic_node",
                            lambda state, **kw: seen.setdefault("critic", kw) and {})
        monkeypatch.setattr(council, "meta_critic_node",
                            lambda state, **kw: seen.setdefault("meta", kw) and {})
        deps = _deps()
        deps.chat_clients = {"critic": object()}
        deps.models = {"critic": "m"}
        graph._wrap_critic(deps)({"lane_idx": 3})
        graph._wrap_meta_critic(deps)({})
        seen["critic"]["tool_executor"]("run_command", "{}", "/x")
        seen["meta"]["tool_executor"]("run_command", "{}", "/x")
        assert deps.tool_executor.calls == [("run_command", "critic:3"),
                                            ("run_command", "meta_critic:0")]


def test_a_judge_with_the_shell_gets_a_lanes_bonus():
    assert council.judge_iters(SPECS) == (14, 13)
    assert council.judge_iters(SPECS[:1]) == (council.ROLE_TOOL_MAX_ITERATIONS,
                                             council.ROLE_TOOL_FORCE_ANSWER_AFTER)


class _Looping:
    """A critic that keeps running commands until its tools are gone."""

    def __init__(self):
        self.calls = []

    def chat(self, payload, **_kw):
        self.calls.append(payload)
        if "tools" not in payload or not payload.get("tools"):
            return {"choices": [{"message": {"role": "assistant",
                                             "content": "DECISION: ready\nran it"},
                                 "finish_reason": "stop"}]}
        n = len(self.calls)
        return {"choices": [{"message": {"role": "assistant", "content": "",
                                         "tool_calls": [{"id": f"c{n}", "type": "function",
                                                         "function": {"name": "run_command",
                                                                      "arguments": json.dumps({"command": f"echo {n}"})}}]},
                             "finish_reason": "tool_calls"}]}


def _state():
    return {"question": "q", "plan": "p", "research": ["REPORT"], "cwd": "",
            "critique": "ok"}


def test_the_critic_is_told_and_given_the_turns():
    c = _Looping()
    council.critic_node(_state(), chat_client=c, model="m", tool_specs=SPECS,
                        tool_executor=_LaneExec(), cwd="/x")
    assert council.JUDGE_SHELL_NOTE in json.dumps(c.calls[0])
    assert 13 <= len(c.calls) <= 16          # 4 turns without the shell


def test_without_the_shell_the_critic_is_unchanged():
    c = _Looping()
    council.critic_node(_state(), chat_client=c, model="m", tool_specs=SPECS[:1],
                        tool_executor=_LaneExec(), cwd="/x")
    assert council.JUDGE_SHELL_NOTE not in json.dumps(c.calls[0])


def test_a_critic_run_counts_as_a_run():
    rows = [{"id": "C1", "n": 1, "role": "critic", "lane": None, "ran": True,
             "command": "bun x vitest run a.test.ts", "output": "Tests 1 passed"}]
    d, _, held = evidence.hold_to_evidence_bar("ready", "DECISION: ready", rows, [])
    assert d == "ready" and not held


def test_the_meta_critic_is_told_too():
    from consultants.engine.storage import RoleTurn
    c = _Looping()
    council.meta_critic_node(
        {**_state(), "research_rounds_used": 1, "turns": [
            RoleTurn(role="critic", round=1, content="DECISION: ready\nok",
                     prompt_tokens=0, completion_tokens=0, duration_seconds=0.0)]},
        chat_client=c, model="m", tool_specs=SPECS, tool_executor=_LaneExec(),
        cwd="/x")
    assert council.JUDGE_SHELL_NOTE in json.dumps(c.calls[0])
