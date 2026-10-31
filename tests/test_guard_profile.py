"""Plan §0.7 / D3 strict: the `[guards] profile` nudge toggle."""
from __future__ import annotations

import json

import pytest

from claude_hooks.agent_loop import runner
from claude_hooks import silent_turns as st
from consultants import config as cc
from consultants.engine import council
from consultants.engine.control import runtime_control_defaults


# --- config, CLI, seed --------------------------------------------------- #

def test_default_is_standard_and_not_seeded():
    cfg = cc.ConsultantsConfig()
    assert cfg.guards.profile == "standard" and cfg.guards.is_default()
    assert "guards" not in runtime_control_defaults(cfg, effort="medium")


def test_seeded_when_strict_anywhere():
    cfg = cc.ConsultantsConfig()
    cfg.guards.roles["planner"] = "strict"
    rc = runtime_control_defaults(cfg, effort="medium")
    assert rc["guards"] == {"profile": "standard", "roles": {"planner": "strict"}}


def test_cli_roundtrip_and_role_override(tmp_path, monkeypatch, capsys):
    from consultants import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["config", "set-guards", "--profile", "strict", "--user"]) == 0
    assert json.loads(capsys.readouterr().out)["guards"] == {
        "profile": "strict", "roles": {}}
    assert cli.main(["config", "set-guards", "--profile", "standard",
                     "--role", "coder", "--user"]) == 0
    capsys.readouterr()
    g = cc.load_config(tmp_path).guards
    assert g.profile == "strict" and g.for_role("coder") == "standard"
    assert g.for_role("researcher") == "strict"
    # Setting the role back to the run-wide profile drops the override.
    cc.set_guards(profile="strict", role="coder", scope="user")
    assert cc.load_config(tmp_path).guards.roles == {}


def test_bad_values_are_refused():
    with pytest.raises(ValueError):
        cc.check_guard_profile("loose")
    with pytest.raises(ValueError):
        cc.set_guards(profile="strict", role="synthesizer")


def test_per_run_override():
    from fastapi import HTTPException
    from consultants.server.app import _apply_guards_override
    cfg = cc.ConsultantsConfig()
    cfg.guards.roles["coder"] = "standard"
    _apply_guards_override(cfg, {"guards": "strict"})
    assert cfg.guards.profile == "strict" and cfg.guards.roles == {}
    with pytest.raises(HTTPException):
        _apply_guards_override(cfg, {"guards": "nope"})


def test_profile_reader():
    assert council._guard_profile({}, "researcher") == "standard"
    state = {"runtime_control": {"guards": {"profile": "strict",
                                            "roles": {"coder": "standard"}}}}
    assert council._guard_profile(state, "researcher") == "strict"
    assert council._guard_profile(state, "coder") == "standard"


# --- the loop ------------------------------------------------------------- #

def _think_only():
    return {"choices": [{"message": {"role": "assistant", "content": "",
                                     "thinking": "hm"},
                         "finish_reason": "stop", "done_reason": "stop"}]}


def _run(profile, script, max_iterations=10):
    events = []
    runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False,
                                 force_answer_after=0, guard_profile=profile,
                                 max_iterations=max_iterations,
                                 on_guard=events.append),
        tool_specs=[{"type": "function", "function": {
            "name": "read_file", "parameters": {"type": "object"}}}],
        chat_fn=lambda p: script.pop(0), tool_executor=lambda n, a, c: "x")
    return events


def test_strict_reasks_thinking_only_three_times():
    std = _run("standard", [_think_only() for _ in range(4)])
    strict = _run("strict", [_think_only() for _ in range(4)])
    count = lambda ev: len([e for e in ev if e.get("kind") == "thinking_only"])
    assert count(std) == st.THINKING_ONLY_RETRY_BUDGET == 2
    assert count(strict) == st.THINKING_ONLY_RETRY_BUDGET_STRICT == 3


def test_strict_warns_two_turns_early():
    assert runner.cap_warning_margin(8) == 1
    assert runner.cap_warning_margin(8, strict=True) == 2
    assert runner.cap_warning_margin(48, strict=True) == 5


# --- the planner ---------------------------------------------------------- #

class _Planner:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, payload, **kw):
        self.calls.append(payload)
        text = self.replies.pop(0)
        return {"choices": [{"message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _plan_state(profile):
    s = council.initial_state(question="q", cwd="/proj",
                              models={"planner": "m"}, topology="council",
                              effort="medium")
    if profile:
        s["runtime_control"] = {"guards": {"profile": profile, "roles": {}}}
    return s


def test_strict_planner_is_reasked_once_when_the_plan_does_not_parse():
    chat = _Planner(["I would look at the cache.", "1. read cache.py\n2. grep ttl"])
    out = council.planner_node(_plan_state("strict"), chat_client=chat, model="m")
    assert len(chat.calls) == 2
    assert chat.calls[1]["messages"][-1]["content"].startswith("[SYSTEM MESSAGE]")
    assert out["plan_items"] == ["read cache.py", "grep ttl"]


def test_standard_planner_is_not_reasked():
    chat = _Planner(["I would look at the cache."])
    council.planner_node(_plan_state(None), chat_client=chat, model="m")
    assert len(chat.calls) == 1


@pytest.mark.parametrize("profile,forced", [(None, False), ("strict", True)])
def test_strict_researcher_lane_opens_with_a_tool_call(profile, forced):
    from types import SimpleNamespace
    seen = {}

    def loop(payload, cwd, *, config, **kw):
        seen["cfg"] = config
        return {"choices": [{"message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}]}

    s = _plan_state(profile)
    s["plan"] = "p"
    council.researcher_node(s, chat_client=SimpleNamespace(chat=lambda p: {}),
                            tool_executor=lambda *a: "", tool_specs=[],
                            grounding_msgs=[], model="m", cwd="/proj",
                            loop_runner=loop)
    assert seen["cfg"].force_first_tool_call is forced
    assert seen["cfg"].guard_profile == (profile or "standard")
