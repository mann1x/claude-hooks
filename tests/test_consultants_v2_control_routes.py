"""M9 route-handler tests for :mod:`consultants.server.control_routes`.

These exercise the FastAPI surface (GET /state, POST /inject /
/control / /interrupt / /resume / /cancel, GET /events) with:

- A real :class:`fastapi.testclient.TestClient`.
- A real ``create_app(run_council=None, ...)``.
- A hand-built SessionState wired with a **fake compiled graph**
  that records ``get_state`` / ``update_state`` / ``invoke`` calls.

The pure-Python payload builders are already covered by
``test_consultants_v2_control_api.py`` (M5, 37 tests). This file
focuses on the HTTP contract: status codes, payload validation,
lifecycle gates (404 / 409 / 410 / 503), and the live-graph
mutation roundtrip.

Skips entirely if FastAPI isn't installed (main env).
"""

from __future__ import annotations

import time
import unittest
from dataclasses import dataclass
from typing import Optional


try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

try:
    # Concrete-leaf probe — a bare ``import langgraph.types`` is fooled by the
    # empty ghost namespace dirs a pip-uninstall leaves behind. See the note in
    # test_consultants_adversary_role.py.
    from langgraph.graph import StateGraph  # noqa: F401
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


# ============================================================== #
# Fakes
# ============================================================== #


@dataclass
class _FakeSnapshot:
    """Mimic LangGraph's ``StateSnapshot`` NamedTuple."""
    values: dict
    next: tuple = ()
    tasks: tuple = ()


class _FakeCompiledGraph:
    """Records every call so tests can assert on payloads.

    Mirrors ``CompiledStateGraph`` for the three methods the route
    handlers touch: ``get_state``, ``update_state``, ``invoke``.
    """

    def __init__(self, snapshot_values: Optional[dict] = None):
        self.snapshot_values = snapshot_values or {
            "runtime_control": {"max_rounds": 1},
            "research": ["round 1 report"],
            "plan_items": ["a", "b"],
            "research_rounds_used": 1,
            "critic_reroutes_used": 0,
            "critic_decision": None,
            "additional_context": [],
            "confidence": [0.65],
            "partial_synthesis": None,
            "final_answer": "",
            "error": None,
        }
        self.update_state_calls: list[dict] = []
        self.invoke_calls: list[dict] = []
        self.raise_on_update = False
        self.raise_on_get = False
        self.raise_on_invoke = False

    def get_state(self, config):
        if self.raise_on_get:
            raise RuntimeError("graph blew up on get_state")
        return _FakeSnapshot(
            values=dict(self.snapshot_values),
            next=(),
            tasks=(),
        )

    def update_state(self, config, delta, *, as_node=None):
        if self.raise_on_update:
            raise RuntimeError("graph blew up on update_state")
        self.update_state_calls.append({
            "config": dict(config or {}),
            "delta": dict(delta or {}),
            "as_node": as_node,
        })
        # Merge the runtime_control delta into our snapshot so
        # subsequent get_state reflects the mutation.
        if isinstance(delta, dict) and "runtime_control" in delta:
            rc = dict(self.snapshot_values.get("runtime_control") or {})
            rc.update(delta["runtime_control"] or {})
            self.snapshot_values["runtime_control"] = rc

    def invoke(self, command, *, config=None):
        if self.raise_on_invoke:
            raise RuntimeError("graph blew up on invoke")
        self.invoke_calls.append({"command": command, "config": dict(config or {})})
        return self.snapshot_values


class _FakeRecorder:
    """Stand-in for MessageRecorder; only list_runtime_events is
    exercised by the SSE handler."""

    def __init__(self, rows: Optional[list[dict]] = None):
        self.rows = list(rows or [])
        self.list_calls: list[int] = []

    def list_runtime_events(self, *, since_event_id: int = 0,
                              limit: int = 1000):
        self.list_calls.append(since_event_id)
        return [r for r in self.rows if int(r["event_id"]) > since_event_id]


def _install_session(app, sid: str, *,
                       status: str = "running",
                       closed: bool = False,
                       compiled: Optional[_FakeCompiledGraph] = None,
                       recorder: Optional[_FakeRecorder] = None):
    """Build a fully-wired SessionState (matching the real
    dataclass) and register it on app.state.sessions[sid]."""
    from consultants.server.app import SessionState
    s = SessionState(
        sid=sid, cwd="/tmp", question="Q",
        effort="medium", topology="council",
        status=status, closed=closed,
    )
    if compiled is not None:
        s._compiled = compiled
        s._thread_config = {"configurable": {"thread_id": sid}}
    s._recorder = recorder
    app.state.sessions[sid] = s
    return s


def _client():
    """Build a FastAPI TestClient with no runner — the M9 routes
    don't need one (the runner only matters for POST /v1/consult).
    """
    from consultants.server.app import create_app
    app = create_app(run_council=None, start_reaper=False)
    return TestClient(app), app


# ============================================================== #
# GET /state
# ============================================================== #


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class TestGetState(unittest.TestCase):

    def test_404_when_session_unknown(self):
        c, _app = _client()
        r = c.get("/v1/consult/missing/state")
        self.assertEqual(r.status_code, 404)
        self.assertIn("not found", r.json()["detail"].lower())

    def test_returns_snapshot_summary_when_live(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-1", compiled=cg)
        r = c.get("/v1/consult/csl-1/state")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["sid"], "csl-1")
        self.assertEqual(body["status"], "running")
        self.assertFalse(body["closed"])
        self.assertEqual(body["research_count"], 1)
        self.assertEqual(body["plan_items_count"], 2)
        self.assertAlmostEqual(body["latest_confidence"], 0.65)
        # Runtime control round-tripped from the snapshot.
        self.assertEqual(body["runtime_control"]["max_rounds"], 1)

    def test_returns_static_snapshot_when_session_completed_no_graph(self):
        # Session present but _compiled cleared (runner finished
        # + cleanup nuked the handle). GET /state must still 200
        # with whatever's on the SessionState.
        c, app = _client()
        s = _install_session(app, "csl-done", status="completed")
        s.research = ["finding"]
        s.plan_items = ["x"]
        s.final_answer = "DONE"
        r = c.get("/v1/consult/csl-done/state")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["research_count"], 1)
        self.assertEqual(body["plan_items_count"], 1)
        self.assertTrue(body["final_answer_ready"])
        self.assertEqual(body["status"], "completed")

    def test_500_when_get_state_raises(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        cg.raise_on_get = True
        _install_session(app, "csl-boom", compiled=cg)
        r = c.get("/v1/consult/csl-boom/state")
        self.assertEqual(r.status_code, 500)
        self.assertIn("get_state failed", r.json()["detail"])

    def test_real_graph_with_memory_checkpointer_serves_state(self):
        """Regression for #214: pre-fix, ProviderBackedStore.run_council
        compiled the council graph without passing a checkpointer.
        LangGraph's get_state() then raised
        ValueError("No checkpointer set") on every M9 state poll, and
        the route returned HTTP 500 ("get_state failed: No
        checkpointer set"). The fix attaches a MemorySaver in the
        runner so the standard config works.

        This test pins that contract by building a *real* (not
        stubbed) compiled graph with a MemorySaver, installing it
        on the session, and asserting the /state endpoint returns
        200 with a valid snapshot — i.e. the route path that 500'd
        on csl-2026-05-18-1724-0f9f now succeeds."""
        try:
            from langgraph.graph import StateGraph, START, END
            from langgraph.checkpoint.memory import MemorySaver
        except ImportError:
            self.skipTest("langgraph not available")

        # Minimal one-node graph that just sets a field. The
        # control_routes /state endpoint reads from the snapshot's
        # ``values`` dict — same shape regardless of how complex
        # the underlying graph is.
        def _node(state: dict) -> dict:
            return {"plan": "x"}

        sg = StateGraph(dict)
        sg.add_node("only", _node)
        sg.add_edge(START, "only")
        sg.add_edge("only", END)
        compiled = sg.compile(checkpointer=MemorySaver())
        # Run it once so get_state has a snapshot to return.
        thread_config = {"configurable": {"thread_id": "csl-real"}}
        compiled.invoke({"plan": ""}, thread_config)

        c, app = _client()
        from consultants.server.app import SessionState
        s = SessionState(
            sid="csl-real", cwd="/tmp", question="Q",
            effort="medium", topology="council",
            status="completed", closed=False,
        )
        s._compiled = compiled
        s._thread_config = thread_config
        app.state.sessions["csl-real"] = s

        r = c.get("/v1/consult/csl-real/state")
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertEqual(body["sid"], "csl-real")
        # The route surfaces snapshot.values; our node set plan="x".
        # The exact field surface depends on what the route picks
        # out — at minimum, the call must NOT 500 with "No
        # checkpointer set".
        self.assertNotIn("get_state failed", r.text)
        self.assertNotIn("No checkpointer set", r.text)


# ============================================================== #
# POST /inject
# ============================================================== #


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class TestInject(unittest.TestCase):

    def test_happy_path_applies_delta(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-2", compiled=cg)
        r = c.post(
            "/v1/consult/csl-2/inject",
            json={"role": "researcher", "text": "also check GDPR"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "applied")
        self.assertEqual(body["routed"], "in_place")
        self.assertIn("injection_id", body)
        # One update_state call with the inject as_node hint.
        self.assertEqual(len(cg.update_state_calls), 1)
        call = cg.update_state_calls[0]
        self.assertEqual(call["as_node"], "researcher")
        self.assertIn("additional_context", call["delta"])

    def test_400_on_invalid_role(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-bad", compiled=cg)
        r = c.post(
            "/v1/consult/csl-bad/inject",
            json={"role": "nonsense", "text": "x"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(cg.update_state_calls, [])

    def test_400_on_empty_text(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-empty", compiled=cg)
        r = c.post(
            "/v1/consult/csl-empty/inject",
            json={"role": "any", "text": ""},
        )
        self.assertEqual(r.status_code, 400)

    def test_404_unknown_sid(self):
        c, _app = _client()
        r = c.post(
            "/v1/consult/no-such/inject",
            json={"role": "any", "text": "x"},
        )
        self.assertEqual(r.status_code, 404)

    def test_rejected_when_session_completed(self):
        # Uniform contract (2026-05-24): terminal sessions no longer
        # 409 — the inject is accepted (200) with status="rejected".
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-done", status="completed", compiled=cg)
        r = c.post(
            "/v1/consult/csl-done/inject",
            json={"role": "any", "text": "late"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(body["phase_at_apply"], "terminal")
        self.assertIn("reason", body)
        # A rejected inject never touches graph state.
        self.assertEqual(cg.update_state_calls, [])

    def test_rejected_when_session_closed(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-cl", closed=True, compiled=cg)
        r = c.post(
            "/v1/consult/csl-cl/inject",
            json={"role": "any", "text": "x"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(cg.update_state_calls, [])

    def test_pending_when_graph_handles_missing(self):
        # Session is "running" but no _compiled — millisecond window
        # after executor.submit, before the runner attaches. Uniform
        # contract: queue as pending (200), don't 503.
        c, app = _client()
        _install_session(app, "csl-race")  # no compiled
        r = c.post(
            "/v1/consult/csl-race/inject",
            json={"role": "any", "text": "x"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["queue_position"], 1)
        self.assertIn("injection_id", body)


# ============================================================== #
# POST /control
# ============================================================== #


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class TestControl(unittest.TestCase):

    def test_applies_runtime_control_delta(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-3", compiled=cg)
        r = c.post(
            "/v1/consult/csl-3/control",
            json={"runtime_control": {"max_rounds": 5}},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(cg.update_state_calls), 1)
        self.assertEqual(
            cg.update_state_calls[0]["delta"]["runtime_control"]["max_rounds"],
            5,
        )

    def test_400_on_invalid_payload_shape(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-bad", compiled=cg)
        # Missing runtime_control entirely.
        r = c.post("/v1/consult/csl-bad/control", json={"foo": 1})
        self.assertEqual(r.status_code, 400)

    def test_400_on_unknown_runtime_field(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-unk", compiled=cg)
        r = c.post(
            "/v1/consult/csl-unk/control",
            json={"runtime_control": {"made_up_knob": 9}},
        )
        # build_runtime_control_delta should reject unknown kwargs.
        self.assertEqual(r.status_code, 400)

    def test_400_on_invalid_value(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-val", compiled=cg)
        # confidence_target must be 0..1
        r = c.post(
            "/v1/consult/csl-val/control",
            json={"runtime_control": {"confidence_target": 99}},
        )
        self.assertEqual(r.status_code, 400)


# ============================================================== #
# POST /interrupt
# ============================================================== #


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class TestInterrupt(unittest.TestCase):

    def test_flips_pause_requested(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-i", compiled=cg)
        r = c.post(
            "/v1/consult/csl-i/interrupt",
            json={"reason": "need to think"},
        )
        self.assertEqual(r.status_code, 200)
        call = cg.update_state_calls[0]
        self.assertTrue(
            call["delta"]["runtime_control"]["pause_requested"],
        )
        self.assertEqual(
            call["delta"]["runtime_control"]["pause_reason"],
            "need to think",
        )

    def test_default_reason_when_body_omitted(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-i2", compiled=cg)
        r = c.post("/v1/consult/csl-i2/interrupt", json={})
        self.assertEqual(r.status_code, 200)
        call = cg.update_state_calls[0]
        self.assertEqual(
            call["delta"]["runtime_control"]["pause_reason"],
            "user-pause",
        )


# ============================================================== #
# POST /resume
# ============================================================== #


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
@unittest.skipUnless(
    HAVE_LANGGRAPH,
    "resume handler needs langgraph.types.Command (consultants env only)",
)
class TestResume(unittest.TestCase):

    def test_clears_interrupt_and_schedules_resume(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-r", compiled=cg)
        r = c.post(
            "/v1/consult/csl-r/resume",
            json={"value": {"approve": True}},
        )
        self.assertEqual(r.status_code, 200)
        # One update_state call for clear_interrupt.
        self.assertEqual(len(cg.update_state_calls), 1)
        clear_delta = cg.update_state_calls[0]["delta"]
        self.assertIsNone(clear_delta["interrupt_state"])
        self.assertFalse(
            clear_delta["runtime_control"]["pause_requested"],
        )
        # Resume invoke ran via the executor — fake graph records.
        # We poll for the invoke since the executor is real (small
        # ThreadPoolExecutor on app.state.executor).
        for _ in range(50):
            if cg.invoke_calls:
                break
            time.sleep(0.01)
        self.assertEqual(len(cg.invoke_calls), 1)


# ============================================================== #
# POST /cancel
# ============================================================== #


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class TestCancel(unittest.TestCase):

    def test_flips_cancel_requested_on_running(self):
        c, app = _client()
        cg = _FakeCompiledGraph()
        _install_session(app, "csl-c", compiled=cg)
        r = c.post(
            "/v1/consult/csl-c/cancel",
            json={"discard_partial": False},
        )
        self.assertEqual(r.status_code, 200)
        call = cg.update_state_calls[0]
        self.assertTrue(
            call["delta"]["runtime_control"]["cancel_requested"],
        )

    def test_noop_apply_on_completed_session(self):
        # Cancel on completed must not 409 — it's an idempotent
        # signal. We just don't push the cancel delta into the
        # graph (no _compiled), and discard_partial is honored.
        c, app = _client()
        _install_session(app, "csl-done", status="completed")
        r = c.post("/v1/consult/csl-done/cancel", json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_410_on_closed_session(self):
        c, app = _client()
        _install_session(app, "csl-cl", closed=True)
        r = c.post("/v1/consult/csl-cl/cancel", json={})
        self.assertEqual(r.status_code, 410)


# ============================================================== #
# GET /events (SSE) — light coverage; the heavy SSE plumbing tests
# live in test_consultants_v2_events.py against the M4 module.
# ============================================================== #


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class TestEvents(unittest.TestCase):

    def test_404_when_session_unknown(self):
        c, _app = _client()
        r = c.get("/v1/consult/no-such/events")
        self.assertEqual(r.status_code, 404)

    def test_replays_runtime_events_then_terminates_on_completed(self):
        # A "completed" session with persisted events: the handler
        # plays them back, then exits its tail loop. We pull the
        # entire response (not stream) since TestClient buffers.
        c, app = _client()
        rec = _FakeRecorder(rows=[
            {"event_id": 1, "kind": "node_started",
             "payload": {"role": "planner"}},
            {"event_id": 2, "kind": "node_finished",
             "payload": {"role": "planner"}},
        ])
        _install_session(app, "csl-ev",
                         status="completed", recorder=rec)
        r = c.get("/v1/consult/csl-ev/events")
        self.assertEqual(r.status_code, 200)
        body = r.text
        # Both events appear (event ids 1 and 2).
        self.assertIn("id: 1", body)
        self.assertIn("id: 2", body)
        self.assertIn("event: node_started", body)
        self.assertIn("event: node_finished", body)

    def test_last_event_id_header_skips_replayed(self):
        c, app = _client()
        rec = _FakeRecorder(rows=[
            {"event_id": 1, "kind": "k", "payload": {}},
            {"event_id": 2, "kind": "k", "payload": {}},
            {"event_id": 3, "kind": "k", "payload": {}},
        ])
        _install_session(app, "csl-resume",
                         status="completed", recorder=rec)
        r = c.get(
            "/v1/consult/csl-resume/events",
            headers={"Last-Event-ID": "2"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertNotIn("id: 1", body)
        self.assertNotIn("id: 2", body)
        self.assertIn("id: 3", body)

    def test_real_recorder_mirror_rows_stream_through_sse(self):
        """Regression for #214: pre-fix, the engine's
        record_node / record_llm / record_tool calls wrote to the
        ``events`` audit table but never mirrored into
        ``runtime_events``. The SSE endpoint reads from
        ``runtime_events`` (via list_runtime_events), so the stream
        was empty for every session that ever ran. csl-1554's
        ``runtime_events`` table had 0 rows — same for all 6
        sessions with that schema today.

        Post-#214 fix the recorder auto-emits a narrow mirror into
        runtime_events whenever record_node / record_llm /
        record_tool fires. This test pins the contract end-to-end:
        drive a real MessageRecorder, record a few node/llm/tool
        events, install the recorder on a session, and assert the
        /events SSE endpoint replays them."""
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from consultants.engine.recorder import (
            MessageRecorder, RecorderMeta,
        )
        with TemporaryDirectory() as td:
            db_path = Path(td) / "transcript.db"
            meta = RecorderMeta(
                sid="csl-mirror", cwd="/tmp", question="Q?",
                effort="medium", topology="council",
                models={"planner": "m"},
            )
            rec = MessageRecorder(db_path, meta=meta)
            try:
                rec.record_node(role="planner", kind="node_enter")
                rec.record_llm(
                    role="planner", round=1, model="gemma4:31b-cloud",
                    request={"r": 1}, response={"a": 1},
                    prompt_tokens=10, completion_tokens=20,
                    duration_ms=100,
                )
                rec.record_node(
                    role="planner", kind="node_exit", duration_ms=110,
                )
                # Drive the real route through a TestClient.
                c, app = _client()
                _install_session(
                    app, "csl-mirror",
                    status="completed", recorder=rec,
                )
                r = c.get("/v1/consult/csl-mirror/events")
                self.assertEqual(r.status_code, 200, msg=r.text)
                body = r.text
                # All three narrow mirror rows surface as SSE events
                # in order: node_enter, llm_call, node_exit.
                self.assertIn("event: node_enter", body)
                self.assertIn("event: llm_call", body)
                self.assertIn("event: node_exit", body)
                # The narrow payload landed in the SSE data block.
                self.assertIn("gemma4:31b-cloud", body)
                self.assertIn("\"prompt_tokens\": 10", body)
                self.assertIn("\"completion_tokens\": 20", body)
            finally:
                rec.close()


if __name__ == "__main__":
    unittest.main()


# ============================================================== #
# POST /tool-ack — the M-A approval channel's HTTP surface
# ============================================================== #


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class TestToolAck(unittest.TestCase):
    """Answering a parked ``ask_human`` tool call.

    The lane is blocked inside its tool executor, so resolving the
    request IS the resume — nothing re-invokes the graph, exactly like
    /adversary-ack.
    """

    def _park(self, app, sid="csl-1"):
        s = _install_session(app, sid)
        return s, s.tool_approvals.open(
            tool="rent_pod", level="ask_human",
            arguments='{"gpu": "h100"}', cwd="/proj",
            reason="spends money", timeout_s=600,
        )

    def test_allow_resolves_the_request(self):
        c, app = _client()
        _s, req = self._park(app)
        r = c.post("/v1/consult/csl-1/tool-ack", json={"allow": True})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["resolved"])
        self.assertEqual(body["request"]["request_id"], req.request_id)
        self.assertEqual(body["request"]["resolution"], "allowed")
        self.assertEqual(body["pending"], [])

    def test_deny_resolves_the_request(self):
        c, app = _client()
        self._park(app)
        r = c.post("/v1/consult/csl-1/tool-ack", json={"allow": False})
        self.assertEqual(r.json()["request"]["resolution"], "denied")

    def test_allow_is_required(self):
        # No default verdict: guessing either way is the failure this
        # channel exists to prevent.
        c, app = _client()
        self._park(app)
        r = c.post("/v1/consult/csl-1/tool-ack", json={})
        self.assertEqual(r.status_code, 400)

    def test_targets_a_specific_request_id(self):
        c, app = _client()
        s, first = self._park(app)
        second = s.tool_approvals.open(
            tool="other", level="ask_human", arguments="", cwd="/proj",
            reason="r", timeout_s=600,
        )
        r = c.post("/v1/consult/csl-1/tool-ack",
                   json={"allow": True, "request_id": second.request_id})
        self.assertEqual(r.json()["request"]["tool"], "other")
        self.assertEqual([p["request_id"] for p in r.json()["pending"]],
                         [first.request_id])

    def test_acking_nothing_is_a_no_op_not_an_error(self):
        # A duplicate ack after a timeout already denied must not 500.
        c, app = _client()
        _install_session(app, "csl-1")
        r = c.post("/v1/consult/csl-1/tool-ack", json={"allow": True})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["resolved"])

    def test_404_on_unknown_session(self):
        c, _app = _client()
        r = c.post("/v1/consult/missing/tool-ack", json={"allow": True})
        self.assertEqual(r.status_code, 404)

    def test_status_surfaces_the_pending_request(self):
        # The signal a monitor polls for — same discipline as
        # adversary_checkpoint_deadline_ts: present only while parked.
        c, app = _client()
        self._park(app)
        body = c.get("/v1/consult/csl-1").json()
        self.assertIn("pending_tool_approvals", body)
        self.assertEqual(body["pending_tool_approvals"][0]["tool"],
                         "rent_pod")
        self.assertEqual(body["pending_tool_approvals"][0]["level"],
                         "ask_human")

    def test_status_omits_the_key_when_nothing_is_parked(self):
        # M12 parity: a default run's status response must be
        # byte-identical to pre-M-A.
        c, app = _client()
        _install_session(app, "csl-1")
        self.assertNotIn("pending_tool_approvals",
                         c.get("/v1/consult/csl-1").json())

    def test_status_omits_the_key_again_after_the_ack(self):
        c, app = _client()
        self._park(app)
        c.post("/v1/consult/csl-1/tool-ack", json={"allow": True})
        self.assertNotIn("pending_tool_approvals",
                         c.get("/v1/consult/csl-1").json())
