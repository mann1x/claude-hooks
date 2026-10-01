"""Councils survive an engine restart.

Shutdown suspends every running council at its next node boundary, its
checkpoint stays in the session dir, and the next engine resumes it
under the same sid from the in-flight index. Before 2026-10-01 the
production checkpointer was in-memory and a restart lost every council
in flight (``scripts/deploy.py`` had to refuse to restart under one).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from consultants.engine.run_control import RunControl, RunSuspended, gate_node
from consultants.server import inflight


# ---------------------------------------------------------------- #
# RunControl: the suspend verb
# ---------------------------------------------------------------- #

class TestSuspendVerb:

    def test_a_suspended_gate_raises_before_the_node_runs(self):
        rc = RunControl("csl-1")
        ran = []
        node = gate_node(lambda s: ran.append(1) or {}, role="critic",
                         run_control=rc)
        assert node({}) == {}
        rc.request_suspend()
        with pytest.raises(RunSuspended):
            node({})
        assert ran == [1]

    def test_suspend_wakes_a_parked_node(self):
        rc = RunControl("csl-1")
        rc.request_pause()
        out = []
        t = threading.Thread(target=lambda: out.append(rc.check("critic")))
        t.start()
        time.sleep(0.1)
        rc.request_suspend()
        t.join(timeout=5)
        assert out == ["suspend"]

    def test_cancel_wins_over_suspend(self):
        rc = RunControl("csl-1")
        rc.request_cancel()
        assert rc.request_suspend() is False
        assert rc.check("critic") == "cancel"


# ---------------------------------------------------------------- #
# The in-flight index
# ---------------------------------------------------------------- #

class TestInflightIndex:

    def test_register_update_remove(self):
        inflight.register({"sid": "a", "cwd": "/x"})
        inflight.register({"sid": "b", "cwd": "/y"})
        inflight.update("a", state="suspended")
        assert inflight.get("a")["state"] == "suspended"
        assert [r["sid"] for r in inflight.load()] == ["a", "b"]
        inflight.remove("a")
        assert [r["sid"] for r in inflight.load()] == ["b"]

    def test_a_corrupt_index_reads_as_empty(self):
        inflight.index_path().write_text("{not json", encoding="utf-8")
        assert inflight.load() == []
        inflight.register({"sid": "c", "cwd": "/z"})
        assert inflight.get("c") is not None


# ---------------------------------------------------------------- #
# App lifecycle
# ---------------------------------------------------------------- #

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from consultants.engine import storage  # noqa: E402
from consultants.server.app import (  # noqa: E402
    SessionState, create_app, resume_inflight_councils,
    suspend_running_councils,
)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _complete(state, runner_input, final="answer"):
    cwd = Path(runner_input["cwd"])
    storage.write_consultation(storage.ConsultationResult(
        session_id=state.sid,
        created=time.strftime("%Y-%m-%dT%H:%M:%S"),
        question=runner_input["question"], models={"synthesizer": "stub"},
        topology=state.topology, effort=state.effort, final_answer=final,
        turns=[storage.RoleTurn(role="synthesizer", round=1, content=final)],
        duration_seconds=0.1, status="completed", cwd=str(cwd),
        parent_sid=state.parent_sid,
        root_sid=getattr(state, "root_sid", None) or state.sid,
    ), cwd=cwd)
    state.final_answer = final
    state.status = "completed"
    state.finished_at = time.time()


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _suspendable_runner(entered: threading.Event):
    """Behaves like the real runner at a node boundary: stops when a
    suspend is requested and reports ``suspended``."""
    def run(state, runner_input):
        entered.set()
        while not state.run_control.suspended:
            time.sleep(0.01)
        state.status = "suspended"
    return run


class TestLifecycle:

    def test_a_run_is_indexed_while_it_runs_and_dropped_after(self, project_dir):
        gate = threading.Event()

        def run(state, runner_input):
            gate.wait(5)
            _complete(state, runner_input)

        client = TestClient(create_app(run_council=run, start_reaper=False))
        sid = client.post("/v1/consult", json={
            "message": "q", "cwd": str(project_dir)}).json()["sid"]
        rec = inflight.get(sid)
        assert rec["kind"] == "consult" and rec["cwd"] == str(project_dir)
        gate.set()
        assert _wait(lambda: inflight.get(sid) is None)

    def test_shutdown_suspends_and_marks_the_record(self, project_dir):
        entered = threading.Event()
        app = create_app(run_council=_suspendable_runner(entered),
                         start_reaper=False)
        client = TestClient(app)
        sid = client.post("/v1/consult", json={
            "message": "q", "cwd": str(project_dir)}).json()["sid"]
        assert entered.wait(5)
        assert suspend_running_councils(app, grace_s=5) == 0
        assert app.state.sessions[sid].status == "suspended"
        assert inflight.get(sid)["state"] == "suspended"

    def test_a_run_still_inside_a_node_after_the_grace_is_reported(self, project_dir):
        release = threading.Event()

        def stuck(state, runner_input):
            release.wait(10)   # an LLM call that ignores the suspend
            state.status = "suspended"

        app = create_app(run_council=stuck, start_reaper=False)
        client = TestClient(app)
        sid = client.post("/v1/consult", json={
            "message": "q", "cwd": str(project_dir)}).json()["sid"]
        fut = app.state.runner_futures[sid]
        try:
            assert suspend_running_councils(app, grace_s=0.2) == 1
            assert app.state.shutdown_unfinished == 1
            assert inflight.get(sid)["state"] == "interrupted"
        finally:
            release.set()
            # Join before the test's env override is undone: the runner
            # settles its record on the way out, and a late settle would
            # write to the real index.
            fut.result(timeout=10)

    def test_startup_resumes_under_the_same_sid(self, project_dir):
        inflight.register({
            "sid": "csl-left", "kind": "consult", "cwd": str(project_dir),
            "question": "unfinished", "effort": "high",
            "extra_roots": [], "root_sid": "csl-left",
            "started_at": time.time() - 600, "state": "suspended",
        })
        seen = {}

        def run(state, runner_input):
            seen["sid"], seen["input"] = state.sid, runner_input
            _complete(state, runner_input)

        app = create_app(run_council=run, start_reaper=False,
                         resume_inflight=True)
        assert _wait(lambda: "sid" in seen)
        assert seen["sid"] == "csl-left"
        assert seen["input"]["resume"] is True
        assert seen["input"]["question"] == "unfinished"
        assert seen["input"]["config"].effort == "high"
        assert _wait(lambda: inflight.get("csl-left") is None)
        assert app.state.sessions["csl-left"].status == "completed"

    def test_a_follow_up_resumes_with_its_parent(self, project_dir):
        app = create_app(run_council=_complete, run_follow_up=_complete,
                         start_reaper=False)
        client = TestClient(app)
        root = client.post("/v1/consult", json={
            "message": "q", "cwd": str(project_dir)}).json()["sid"]
        assert _wait(lambda: app.state.sessions[root].status == "completed")

        inflight.register({
            "sid": "csl-child", "kind": "follow-up", "cwd": str(project_dir),
            "question": "and then?", "effort": "medium",
            "parent_sid": root, "root_sid": root,
            "started_at": time.time(),
        })
        seen = {}

        def run_fu(state, runner_input):
            seen["parent"] = runner_input["parent_state"]
            _complete(state, runner_input)

        app2 = create_app(run_council=_complete, run_follow_up=run_fu,
                          start_reaper=False, resume_inflight=True)
        assert _wait(lambda: "parent" in seen)
        assert seen["parent"].sid == root
        assert root in app2.state.sessions   # for the ready-to-review flip

    def test_a_council_that_keeps_failing_to_resume_is_given_up(self, project_dir):
        inflight.register({
            "sid": "csl-loop", "kind": "consult", "cwd": str(project_dir),
            "question": "q", "effort": "medium",
            "resumes": inflight.MAX_RESUMES, "started_at": time.time(),
        })
        calls = []
        app = create_app(run_council=lambda s, i: calls.append(s.sid),
                         start_reaper=False)
        assert resume_inflight_councils(app) == []
        assert calls == [] and inflight.get("csl-loop") is None

    def test_a_record_whose_cwd_is_gone_is_dropped(self, tmp_path):
        inflight.register({"sid": "csl-gone", "kind": "consult",
                           "cwd": str(tmp_path / "nope"), "question": "q"})
        app = create_app(run_council=_complete, start_reaper=False)
        assert resume_inflight_councils(app) == []
        assert inflight.get("csl-gone") is None

    def test_a_finished_run_discards_its_checkpoint(self, project_dir):
        pytest.importorskip("langgraph.checkpoint.sqlite")
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_checkpointer, sqlite_checkpoint_path,
        )

        def run(state, runner_input):
            state._checkpointer_handle = make_checkpointer(
                CheckpointerConfig(), Path(runner_input["cwd"]), state.sid)
            _complete(state, runner_input)

        client = TestClient(create_app(run_council=run, start_reaper=False))
        sid = client.post("/v1/consult", json={
            "message": "q", "cwd": str(project_dir)}).json()["sid"]
        db = sqlite_checkpoint_path(project_dir, sid)
        assert _wait(lambda: inflight.get(sid) is None)
        assert _wait(lambda: not db.exists())

    def test_health_advertises_suspension(self):
        client = TestClient(create_app(run_council=None, start_reaper=False))
        assert client.get("/v1/health").json()["suspends_on_shutdown"] is True


# ---------------------------------------------------------------- #
# Engine: a real council graph, suspended and resumed
# ---------------------------------------------------------------- #

def _resp(content):
    return {"choices": [{"message": {"role": "assistant",
                                     "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class _Stub:
    def __init__(self, content, on_call=None):
        self.content, self.on_call, self.calls = content, on_call, 0

    def chat(self, payload, *, think=True):
        self.calls += 1
        if self.on_call:
            self.on_call()
        return _resp(self.content)


class TestEngineResume:

    def _graph(self, project_dir, sid, state, clients):
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_checkpointer,
        )
        from consultants.engine.graph import GraphDeps, build_council_graph
        from consultants.engine.state_v2 import make_checkpointer_serde
        handle = make_checkpointer(CheckpointerConfig(), project_dir, sid,
                                   serde=make_checkpointer_serde())
        roles = ("planner", "researcher", "critic", "synthesizer")
        deps = GraphDeps(
            chat_clients=clients, models={r: "m" for r in roles},
            enabled_roles=roles, cwd=str(project_dir),
            tool_executor=lambda *a, **kw: "", tool_specs=[],
            grounding_msgs=[], disable_cache=True,
            run_control=state.run_control,
        )
        compiled = build_council_graph(
            deps, checkpointer=handle.saver,
            interrupt_before=["synthesizer"])
        return compiled, handle

    def test_a_suspended_council_resumes_where_it_stopped(self, project_dir):
        pytest.importorskip("langgraph.checkpoint.sqlite")
        from consultants.engine import council as council_mod
        from consultants.server.runner import _drive_council_stream

        sid = "csl-durable"
        roles = ["planner", "researcher", "critic", "synthesizer"]
        tc = {"configurable": {"thread_id": sid}}
        initial = council_mod.initial_state(
            question="q", cwd=str(project_dir), models={},
            topology="council", effort="high")

        # Engine 1: the shutdown lands while the researcher is working.
        s1 = SessionState(sid=sid, cwd=str(project_dir), question="q",
                          effort="high", topology="council",
                          progress={r: "pending" for r in roles})
        c1 = {"planner": _Stub("1. investigate"),
              "researcher": _Stub("findings",
                                  on_call=s1.run_control.request_suspend),
              "critic": _Stub("DECISION: ready"),
              "synthesizer": _Stub("FINAL ANSWER")}
        g1, h1 = self._graph(project_dir, sid, s1, c1)
        with pytest.raises(RunSuspended):
            _drive_council_stream(g1, initial, tc, state=s1, enabled=roles,
                                  review_before_synthesis=False,
                                  recorder=None, log_label="t")
        assert c1["critic"].calls == 0
        h1.close()

        # Engine 2: fresh graph, fresh clients, same checkpoint.
        s2 = SessionState(sid=sid, cwd=str(project_dir), question="q",
                          effort="high", topology="council",
                          progress={r: "pending" for r in roles})
        c2 = {"planner": _Stub("1. investigate"),
              "researcher": _Stub("findings"),
              "critic": _Stub("DECISION: ready"),
              "synthesizer": _Stub("FINAL ANSWER")}
        g2, h2 = self._graph(project_dir, sid, s2, c2)
        try:
            out = _drive_council_stream(g2, initial, tc, state=s2,
                                        enabled=roles,
                                        review_before_synthesis=False,
                                        recorder=None, log_label="t",
                                        resume=True)
        finally:
            h2.close()
        assert "FINAL ANSWER" in out["final_answer"]
        assert c2["planner"].calls == 0, "finished work is not redone"
        assert c2["researcher"].calls == 0
        assert c2["critic"].calls == 1
        assert out["research"] == ["findings"] or any(
            "findings" in r for r in out["research"])
