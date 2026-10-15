"""Plan D9: graceful stop — `wrap-up` ends a run on an answer from what it
has, where `cancel` ends it with none."""
from __future__ import annotations

import pytest

from claude_hooks.agent_loop import runner
from consultants.engine import council, events
from consultants.engine.run_control import RunControl, gate_node, wrap_up_skips
from consultants.engine.state_v2 import ToolResult


# --- the loop ------------------------------------------------------------- #

def _call_turn():
    return {"choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path": "a.py"}'}}]},
        "finish_reason": "tool_calls"}]}


def _report(text="what I found"):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def test_a_running_loop_ends_on_one_report_when_the_run_wraps_up():
    flag = {"on": False}
    sent, guards = [], []

    def chat(payload):
        sent.append(payload)
        if len(sent) == 2:
            flag["on"] = True      # the operator asks while turn 2 runs
        return _call_turn() if len(sent) < 3 else _report()

    final = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False, max_iterations=10,
                                 wrap_up_check=lambda: flag["on"],
                                 on_guard=guards.append),
        tool_specs=[{"type": "function", "function": {
            "name": "read_file", "parameters": {"type": "object"}}}],
        chat_fn=chat, tool_executor=lambda n, a, c: "contents")
    assert len(sent) == 3                       # no work after the request
    last = sent[-1]
    assert "tools" not in last and last["think"] is False
    assert last["messages"][-1]["content"] == runner.WRAP_UP_FINAL
    assert last["messages"][-1]["_harness"] is True
    # The tool result turn 2 earned is in the history the report is asked from.
    assert any(m.get("role") == "tool" for m in last["messages"])
    assert events.loop_stop(final) == ("wrap_up", "wrap_up")
    assert guards[-1]["guard"] == "wrap_up"


def test_no_check_and_a_raising_check_change_nothing():
    for check in (None, lambda: 1 / 0):
        sent = []
        final = runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "q"}]},
            "/tmp", config=runner.LoopConfig(force_first_tool_call=False,
                                             wrap_up_check=check),
            tool_specs=[], chat_fn=lambda p: sent.append(p) or _report(),
            tool_executor=lambda *a: "")
        assert len(sent) == 1 and events.loop_stop(final) == ("completed", None)


# --- RunControl ----------------------------------------------------------- #

def test_wrap_up_is_recorded_once_and_releases_a_pause():
    rc = RunControl("csl-w")
    assert rc.partial() is None and "wrap_up_requested" not in rc.snapshot()
    rc.request_pause()
    assert rc.request_wrap_up("enough", now=100.0) is True
    assert rc.request_wrap_up("again") is False
    assert not rc.paused and rc.wrapping_up
    assert rc.snapshot()["wrap_up_reason"] == "enough"
    assert rc.partial() == {"reason": "wrap_up", "requested_by": "enough",
                            "requested_at": 100.0}


def test_a_cancelled_run_has_nothing_to_wrap_up():
    rc = RunControl("csl-w")
    rc.request_cancel()
    assert rc.request_wrap_up() is False and not rc.wrapping_up
    rc2 = RunControl("csl-w2")
    rc2.request_wrap_up()
    rc2.request_cancel()                 # cancel still wins
    assert not rc2.wrapping_up and rc2.partial() is None


def test_what_the_gate_skips():
    for role in ("planner", "critic", "meta_critic", "xauto_escalator",
                 "adversary", "tool_executor", "coder"):
        assert wrap_up_skips(role, {})
    assert not wrap_up_skips("synthesizer", {})
    assert wrap_up_skips("researcher", {"research_rounds_used": 0})
    # A REPORT-mode re-entry holds paid-for results of its round: it runs.
    res = ToolResult(intent="i", content="o", lane_idx=0, parent_round=1)
    assert not wrap_up_skips("researcher", {"research_rounds_used": 0,
                                            "tool_results": [res]})
    assert wrap_up_skips("researcher", {"research_rounds_used": 1,
                                        "tool_results": [res]})


def test_the_gate_skips_and_names_the_lane():
    rc = RunControl("csl-w")
    seen, ran = [], []
    lane = gate_node(lambda s: ran.append(s) or {"research": ["x"]},
                     role="researcher", run_control=rc,
                     emit=lambda k, p: seen.append((k, p)))
    rc.request_wrap_up()
    assert lane({"lane_idx": 2}) == {} and ran == []
    assert rc.wrap_up_skipped == ["researcher#2"]
    assert seen[0][0] == "node_wrap_up_skipped"
    assert rc.partial()["skipped_roles"] == ["researcher#2"]


# --- a real council graph -------------------------------------------------- #

class _Stub:
    def __init__(self, content, on_call=None):
        self.content, self.on_call, self.payloads = content, on_call, []

    def chat(self, payload, *, think=True):
        self.payloads.append(payload)
        if self.on_call:
            self.on_call()
        return {"choices": [{"message": {"role": "assistant",
                                         "content": self.content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _run(tmp_path, rc, clients):
    from consultants.engine.graph import GraphDeps, build_council_graph
    roles = ("planner", "researcher", "critic", "synthesizer")
    deps = GraphDeps(chat_clients=clients, models={r: "m" for r in roles},
                     enabled_roles=roles, cwd=str(tmp_path),
                     tool_executor=lambda *a, **kw: "", tool_specs=[],
                     grounding_msgs=[], disable_cache=True, run_control=rc)
    state = council.initial_state(question="q", cwd=str(tmp_path), models={},
                                  topology="council", effort="high")
    return build_council_graph(deps).invoke(state)


def _synth_prompt(stub):
    return stub.payloads[-1]["messages"][-1]["content"]


def test_a_wrap_up_during_research_goes_straight_to_a_partial_answer(tmp_path):
    rc = RunControl("csl-w")
    clients = {"planner": _Stub("1. look at a\n2. look at b\n3. look at c"),
               "researcher": _Stub("findings", on_call=rc.request_wrap_up),
               "critic": _Stub("DECISION: needs_more_research"),
               "synthesizer": _Stub("PARTIAL ANSWER")}
    out = _run(tmp_path, rc, clients)
    assert out["final_answer"].startswith("PARTIAL ANSWER")
    assert clients["critic"].payloads == []          # no review round
    assert council.WRAP_UP_SYNTHESIS_NOTE in _synth_prompt(clients["synthesizer"])
    assert rc.partial()["reason"] == "wrap_up"


def test_a_wrap_up_before_anything_ran_still_answers(tmp_path):
    rc = RunControl("csl-w")
    rc.request_wrap_up()
    clients = {"planner": _Stub("1. a\n2. b\n3. c"),
               "researcher": _Stub("findings"),
               "critic": _Stub("DECISION: ready"),
               "synthesizer": _Stub("ANSWER FROM NOTHING")}
    out = _run(tmp_path, rc, clients)
    assert out["final_answer"].startswith("ANSWER FROM NOTHING")
    assert [c for c in ("planner", "researcher", "critic")
            if clients[c].payloads] == []
    assert {"planner", "researcher", "critic"} <= {
        s.split("#")[0] for s in rc.partial()["skipped_roles"]}


def test_without_a_wrap_up_the_graph_is_unchanged(tmp_path):
    rc = RunControl("csl-w")
    clients = {"planner": _Stub("1. a\n2. b\n3. c"),
               "researcher": _Stub("findings"),
               "critic": _Stub("DECISION: ready"),
               "synthesizer": _Stub("ANSWER")}
    _run(tmp_path, rc, clients)
    assert clients["critic"].payloads
    assert council.WRAP_UP_SYNTHESIS_NOTE not in _synth_prompt(clients["synthesizer"])
    assert rc.partial() is None


# --- the result ----------------------------------------------------------- #

def test_the_summary_says_the_answer_is_partial():
    from consultants.engine import storage
    r = storage.ConsultationResult(
        session_id="csl-w", created="2026-10-15T10:00:00", question="q",
        models={}, topology="council", effort="high", final_answer="A",
        turns=[], duration_seconds=1.0, status="completed",
        partial={"reason": "wrap_up", "requested_by": "x",
                 "requested_at": 1.0})
    assert "\npartial: wrap_up\n" in storage.render_summary(r)
    assert '"partial"' in storage.render_metadata(r)
    plain = storage.ConsultationResult(
        session_id="csl-w", created="c", question="q", models={},
        topology="council", effort="high", final_answer="A", turns=[],
        duration_seconds=1.0, status="completed")
    assert "partial:" not in storage.render_summary(plain)


def test_route_and_cli_verb(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from consultants.server.app import SessionState, create_app
    app = create_app()
    s = SessionState(sid="csl-w", cwd=str(tmp_path), question="q",
                     effort="high", topology="council", progress={})
    s.status = "running"
    app.state.sessions["csl-w"] = s
    c = TestClient(app)
    r = c.post("/v1/consult/csl-w/wrap-up", json={"reason": "enough"})
    assert r.status_code == 200 and r.json()["wrap_up_accepted"] is True
    assert s.run_control.wrapping_up
    s2 = SessionState(sid="csl-c", cwd=str(tmp_path), question="q",
                      effort="high", topology="council", progress={})
    s2.status = "running"
    s2.run_control.request_cancel()
    app.state.sessions["csl-c"] = s2
    assert c.post("/v1/consult/csl-c/wrap-up").status_code == 409

    from consultants import cli
    args = cli.build_parser().parse_args(["wrap-up", "csl-w", "--reason", "r"])
    assert args.fn is cli.cmd_wrap_up


def test_a_critic_asking_for_more_research_is_overruled_by_a_wrap_up(tmp_path):
    rc = RunControl("csl-w")
    clients = {"planner": _Stub("1. a\n2. b\n3. c"),
               "researcher": _Stub("findings"),
               "critic": _Stub("DECISION: needs_more_research\nGAPS: b",
                               on_call=rc.request_wrap_up),
               "synthesizer": _Stub("ANSWER")}
    _run(tmp_path, rc, clients)
    # Straight to the synthesizer: no second round is even entered. (The
    # critic's verdict stays "needs more research"; without the routing
    # guard the skipped researcher and critic looped until LangGraph's
    # recursion limit.)
    assert not [n for n in rc.wrap_up_skipped if n.startswith("researcher")]
    assert council.WRAP_UP_SYNTHESIS_NOTE in _synth_prompt(clients["synthesizer"])


def test_a_cancel_during_a_critic_asking_for_more_research_drains(tmp_path):
    """The same routing hazard, pre-dating D9: the skipped researcher and
    critic left the critic's last verdict standing, and the run went round
    them until LangGraph's recursion limit — "graph crashed", not
    "cancelled"."""
    rc = RunControl("csl-c")
    clients = {"planner": _Stub("1. a\n2. b\n3. c"),
               "researcher": _Stub("findings"),
               "critic": _Stub("DECISION: needs_more_research\nGAPS: b",
                               on_call=rc.request_cancel),
               "synthesizer": _Stub("ANSWER")}
    out = _run(tmp_path, rc, clients)
    assert not out.get("final_answer")             # cancel: no answer
    assert clients["synthesizer"].payloads == []
    assert "researcher" not in rc.skipped           # no round 2 entered
