"""Plan D9: retry-lanes — a finished run's researcher lanes that failed for
a reason the question did not cause, run again as a child session."""
from __future__ import annotations

import threading

import pytest

from consultants.engine import council
from consultants.engine import retry_lanes as rl


def _stop(lane, fc, rnd=1, role="researcher"):
    row = {"role": role, "round": rnd, "lane_idx": lane, "stop_reason": fc or "completed"}
    if fc:
        row["failure_class"] = fc
    return row


def test_which_lanes_are_retried():
    stops = [_stop(0, None), _stop(1, "infra"), _stop(2, "task_error"),
             _stop(3, "model_missing"), _stop(4, "context_overflow"),
             _stop(5, "cancelled"), _stop(6, "loop_guard"),
             _stop(0, "infra", rnd=2),                  # a re-route lane
             _stop(1, "infra", role="tool_executor")]
    assert rl.failed_lanes(stops) == [1, 3, 4]
    # The last exit of a lane decides.
    assert rl.failed_lanes([_stop(1, "infra"), _stop(1, None)]) == []
    assert rl.failed_lanes([_stop(None, "infra")]) == [0]


def test_lanes_are_rebuilt_as_the_fan_out_built_them(monkeypatch):
    monkeypatch.setattr(council, "FANOUT_MAX_LANES", 3)
    items = ["read a", "read b", "read c"]
    groups = council.group_items_into_lanes(items, 3)
    specs = rl.retry_specs([1, 4], plan="", plan_items=items,
                           models_per_lane=["m0", "m1"])
    assert specs == [
        {"lane_idx": 1, "plan_item": council.join_lane_items(groups[0]),
         "model": "m1"},
        {"lane_idx": 4, "plan_item": council.join_lane_items(groups[2]),
         "model": "m0"}]
    # Outside the rebuilt fan-out: left out, never guessed.
    assert rl.retry_specs([9], plan="", plan_items=items,
                          models_per_lane=["m0"]) == []
    # Plan items recovered from the plan text of a disk-reopened parent.
    assert rl.retry_specs([0], plan="1. read a\n2. read b", plan_items=None,
                          models_per_lane=["m0"])[0]["plan_item"]
    # Below the fan-out threshold the one lane was asked the whole plan.
    assert rl.retry_specs([0], plan="just look", plan_items=["just look"],
                          models_per_lane=["m0"]) == [
        {"lane_idx": 0, "plan_item": None, "model": "m0"}]


def test_tombstones_are_replaced_not_kept():
    assert rl.surviving_reports([
        "found X", "(researcher lane failed: ConnectionError)",
        "  (researcher lane produced no findings within the budget)", "",
        "found Y"]) == ["found X", "found Y"]


def test_lane_stops_reads_the_d7_rows(tmp_path):
    from consultants.engine.recorder import MessageRecorder, RecorderMeta
    rec = MessageRecorder(tmp_path / "transcript.db", meta=RecorderMeta(
        sid="csl-r", cwd=str(tmp_path), question="q", effort="medium",
        topology="council", models={}))
    rec.record_node(role="researcher", kind="node_exit", lane_idx=2,
                    duration_ms=1, error="boom", stop_reason="infra",
                    failure_class="infra")
    rec.close()
    assert rl.failed_lanes(rl.lane_stops(tmp_path / "transcript.db")) == [2]


# --- the follow-up graph --------------------------------------------------- #

class _Stub:
    def __init__(self, content):
        self.content, self.payloads = content, []
        self._lock = threading.Lock()

    def chat(self, payload, *, think=True):
        with self._lock:
            self.payloads.append(payload)
        return {"choices": [{"message": {"role": "assistant",
                                         "content": self.content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def test_the_retry_graph_runs_only_the_failed_lanes(tmp_path):
    from consultants.engine.graph import GraphDeps, build_follow_up_graph
    clients = {"researcher": _Stub("RETRIED FINDING"),
               "synthesizer": _Stub("NEW ANSWER")}
    roles = ("researcher", "synthesizer")
    deps = GraphDeps(chat_clients=clients, models={r: "m" for r in roles},
                     enabled_roles=roles, cwd=str(tmp_path),
                     tool_executor=lambda *a, **kw: "", tool_specs=[],
                     grounding_msgs=[], disable_cache=True)
    specs = [{"lane_idx": 1, "plan_item": "Sub-research lane 1: read b",
              "model": "m1"},
             {"lane_idx": 3, "plan_item": "Sub-research lane 3: read d",
              "model": "m1"}]
    state = council.initial_state(question="the question", cwd=str(tmp_path),
                                  models={}, topology="council",
                                  effort="medium")
    state["plan"] = "1. read a\n2. read b\n3. read c\n4. read d"
    state["research"] = ["SURVIVING REPORT"]
    out = build_follow_up_graph(deps, retry_lanes=specs).invoke(state)
    asked = [p["messages"][-1]["content"]
             for p in clients["researcher"].payloads]
    assert len(asked) == 2
    assert any("read b" in a for a in asked) and any("read d" in a for a in asked)
    assert {p["model"] for p in clients["researcher"].payloads} == {"m1"}
    synth = clients["synthesizer"].payloads[-1]["messages"][-1]["content"]
    assert "SURVIVING REPORT" in synth and synth.count("RETRIED FINDING") == 2
    assert out["final_answer"].startswith("NEW ANSWER")


# --- the route ------------------------------------------------------------ #

def _parent(tmp_path, app, *, fail_class="infra"):
    from consultants.engine import storage
    from consultants.engine.recorder import MessageRecorder, RecorderMeta
    from consultants.server.app import SessionState
    sid = "csl-parent"
    sdir = storage.session_dir(tmp_path, sid)
    sdir.mkdir(parents=True)
    rec = MessageRecorder(sdir / storage.TRANSCRIPT_DB_FILENAME,
                          meta=RecorderMeta(sid=sid, cwd=str(tmp_path),
                                            question="q", effort="medium",
                                            topology="council", models={}))
    rec.record_node(role="researcher", kind="node_exit", lane_idx=0,
                    duration_ms=1, stop_reason="completed")
    rec.record_node(role="researcher", kind="node_exit", lane_idx=1,
                    duration_ms=1, stop_reason=fail_class,
                    failure_class=fail_class)
    rec.close()
    s = SessionState(sid=sid, cwd=str(tmp_path), question="the question",
                     effort="medium", topology="council", progress={})
    s.status = "completed"
    s.plan = "1. read a\n2. read b\n3. read c"
    s.plan_items = ["read a", "read b", "read c"]
    s.research = ["found a", "(researcher lane failed: ConnectionError)"]
    s.models = {"researcher": "m0"}
    app.state.sessions[sid] = s
    return s


def test_the_route_starts_a_child_for_the_failed_lanes(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from consultants.server.app import create_app
    got = {}
    done = threading.Event()

    def run_follow_up(state, runner_input):
        got.update(runner_input)
        state.status = "completed"
        done.set()

    app = create_app(run_follow_up=run_follow_up, start_reaper=False)
    parent = _parent(tmp_path, app)
    count = parent.followup_count
    r = TestClient(app).post("/v1/consult/csl-parent/retry-lanes",
                             json={"cwd": str(tmp_path)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["sid"] and body["parent_sid"] == "csl-parent"
    assert [s["lane_idx"] for s in body["retry_lanes"]] == [1]
    assert done.wait(5)
    assert got["question"] == "the question"
    assert got["retry_lanes"][0]["model"] == "m0"
    assert parent.followup_count == count       # not a follow-up round


def test_the_route_says_when_nothing_is_retryable(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from consultants.server.app import create_app
    app = create_app(run_follow_up=lambda s, i: None, start_reaper=False)
    _parent(tmp_path, app, fail_class="task_error")
    c = TestClient(app)
    body = c.post("/v1/consult/csl-parent/retry-lanes",
                  json={"cwd": str(tmp_path)}).json()
    assert body["ok"] is False and body["reason"] == "no_retryable_lanes"
    assert c.post("/v1/consult/csl-parent/retry-lanes",
                  json={"cwd": str(tmp_path), "lanes": [0]}).status_code == 400


def test_cli_verb():
    from consultants import cli
    args = cli.build_parser().parse_args(["retry-lanes", "csl-x", "--lanes", "1,3"])
    assert args.fn is cli.cmd_retry_lanes and args.lanes == "1,3"
