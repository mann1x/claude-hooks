"""M9 inject-status contract tests (safe-layer, 2026-05-24).

Covers the uniform-status /inject redesign:

1. **Pure helpers** — :func:`classify_phase`, :func:`default_target_for_phase`
   (no I/O; map a snapshot's pending-node set to a council phase + route).
2. **SessionState registry** — ``enqueue_injection`` / ``drain_injections`` /
   ``injection_records`` + content_hash idempotency + the RLock guard.
3. **HTTP contract** — POST /inject always 200 + a status in the body
   (applied|pending|rejected|failed), phase-aware routing, the spin-up
   pending→drain path, terminal→rejected, update_state-raises→failed, and
   the idempotent retry. GET /state surfaces the injections array. The SSE
   stream emits a terminal ``event: complete`` frame.
4. **Regression guard** — run_follow_up passes ``checkpointer=`` to
   build_follow_up_graph (the defect that 500'd /inject + /state on every
   follow-up). Source-inspection guard in the established repo style
   (cf. tests/test_popen_helpers.py).

Skips the HTTP cohort when FastAPI isn't installed (main env); the pure +
source-inspection cohorts run everywhere.
"""

from __future__ import annotations

import inspect
import re
import unittest
from dataclasses import dataclass
from typing import Optional


try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

try:
    import langgraph  # noqa: F401
    from langgraph.checkpoint.memory import MemorySaver  # noqa: F401
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


from consultants.server.control import (
    INJECT_STATUS_APPLIED,
    INJECT_STATUS_FAILED,
    INJECT_STATUS_PENDING,
    INJECT_STATUS_REJECTED,
    Injection,
    PHASE_CRITIC,
    PHASE_PLANNING,
    PHASE_RESEARCH,
    PHASE_SYNTHESIS,
    PHASE_TERMINAL,
    PHASE_UNKNOWN,
    ROUTED_IN_PLACE,
    ROUTED_QUEUED,
    classify_phase,
    default_target_for_phase,
)


# ============================================================== #
# 1. Pure phase classification + routing
# ============================================================== #

class TestClassifyPhase(unittest.TestCase):

    def test_synthesis_beats_everything(self):
        self.assertEqual(
            classify_phase(next_nodes=["critic", "synthesizer"],
                           status="running", closed=False),
            PHASE_SYNTHESIS,
        )

    def test_critic(self):
        self.assertEqual(
            classify_phase(next_nodes=["critic"], status="running",
                           closed=False),
            PHASE_CRITIC,
        )

    def test_research_variants(self):
        for node in ("researcher", "tool_executor", "coder", "coder_router"):
            self.assertEqual(
                classify_phase(next_nodes=[node], status="running",
                               closed=False),
                PHASE_RESEARCH, msg=node,
            )

    def test_planning(self):
        self.assertEqual(
            classify_phase(next_nodes=["planner"], status="running",
                           closed=False),
            PHASE_PLANNING,
        )

    def test_terminal_on_status(self):
        for st in ("completed", "failed"):
            self.assertEqual(
                classify_phase(next_nodes=["synthesizer"], status=st,
                               closed=False),
                PHASE_TERMINAL, msg=st,
            )

    def test_terminal_on_closed(self):
        self.assertEqual(
            classify_phase(next_nodes=["researcher"], status="running",
                           closed=True),
            PHASE_TERMINAL,
        )

    def test_empty_next_running_is_unknown(self):
        self.assertEqual(
            classify_phase(next_nodes=[], status="running", closed=False),
            PHASE_UNKNOWN,
        )
        self.assertEqual(
            classify_phase(next_nodes=None, status="running", closed=False),
            PHASE_UNKNOWN,
        )


class TestDefaultTarget(unittest.TestCase):

    def test_phase_defaults(self):
        self.assertEqual(default_target_for_phase(PHASE_PLANNING), "planner")
        self.assertEqual(default_target_for_phase(PHASE_RESEARCH), "researcher")
        self.assertEqual(default_target_for_phase(PHASE_CRITIC), "critic")
        # Synthesis routes at researcher semantically (rewind intent).
        self.assertEqual(default_target_for_phase(PHASE_SYNTHESIS), "researcher")
        self.assertEqual(default_target_for_phase(PHASE_UNKNOWN), "researcher")

    def test_unknown_phase_falls_back_to_researcher(self):
        self.assertEqual(default_target_for_phase("bogus"), "researcher")


# ============================================================== #
# 2. SessionState injection registry
# ============================================================== #

class TestInjectionRegistry(unittest.TestCase):

    def _state(self):
        from consultants.server.app import SessionState
        return SessionState(sid="t", cwd="/x", question="q",
                            effort="medium", topology="council")

    def test_enqueue_returns_position_and_dedups(self):
        s = self._state()
        a = Injection(id="a", role="any", text="hello", content_hash="h1")
        self.assertEqual(s.enqueue_injection(a), 1)
        self.assertEqual(a.status, INJECT_STATUS_PENDING)
        self.assertEqual(a.routed, ROUTED_QUEUED)
        b = Injection(id="b", role="any", text="hello", content_hash="h1")
        # Same content_hash → no duplicate; returns existing position.
        self.assertEqual(s.enqueue_injection(b), 1)
        self.assertEqual(len(s._injections), 1)
        self.assertEqual(len(s._pending_injections), 1)

    def test_drain_applies_and_clears_pending(self):
        s = self._state()
        s.enqueue_injection(
            Injection(id="a", role="any", text="x", content_hash="h1"))

        def apply_fn(inj):
            inj.status = INJECT_STATUS_APPLIED
            inj.routed = ROUTED_IN_PLACE
            inj.target_role = "researcher"

        drained = s.drain_injections(apply_fn)
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0].status, INJECT_STATUS_APPLIED)
        self.assertEqual(len(s._pending_injections), 0)

    def test_drain_keeps_still_pending(self):
        s = self._state()
        s.enqueue_injection(
            Injection(id="a", role="any", text="x", content_hash="h1"))

        def apply_fn(inj):
            # Simulate "graph still not ready" — leave pending.
            inj.status = INJECT_STATUS_PENDING

        drained = s.drain_injections(apply_fn)
        self.assertEqual(drained, [])
        self.assertEqual(len(s._pending_injections), 1)

    def test_drain_marks_failed_when_apply_raises(self):
        s = self._state()
        s.enqueue_injection(
            Injection(id="a", role="any", text="x", content_hash="h1"))

        def apply_fn(inj):
            raise RuntimeError("boom")

        drained = s.drain_injections(apply_fn)
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0].status, INJECT_STATUS_FAILED)
        self.assertIn("boom", drained[0].error)
        self.assertEqual(len(s._pending_injections), 0)

    def test_injection_records_shape(self):
        s = self._state()
        s.register_injection(
            Injection(id="a", role="researcher", text="x",
                      status=INJECT_STATUS_APPLIED, routed=ROUTED_IN_PLACE,
                      target_role="researcher", phase_at_apply="research"))
        recs = s.injection_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["id"], "a")
        self.assertEqual(recs[0]["status"], "applied")
        self.assertEqual(recs[0]["routed"], "in_place")
        self.assertEqual(recs[0]["phase_at_apply"], "research")


# ============================================================== #
# 3. HTTP inject contract
# ============================================================== #

@dataclass
class _Snapshot:
    values: dict
    next: tuple = ()
    tasks: tuple = ()


class _FakeGraph:
    """Configurable fake: ``next_nodes`` drives phase classification."""

    def __init__(self, *, next_nodes=(), values=None):
        self._next = tuple(next_nodes)
        self._values = values or {
            "runtime_control": {"max_rounds": 3, "max_reroutes": 2},
            "research": [], "plan_items": [],
            "research_rounds_used": 1, "critic_reroutes_used": 0,
            "additional_context": [], "confidence": [],
            "final_answer": "", "error": None,
        }
        self.update_state_calls = []
        self.raise_on_get = False
        self.raise_on_update = False

    def get_state(self, config):
        if self.raise_on_get:
            raise RuntimeError("get_state blew up")
        return _Snapshot(values=dict(self._values), next=self._next, tasks=())

    def update_state(self, config, delta, *, as_node=None):
        if self.raise_on_update:
            raise RuntimeError("update_state blew up")
        self.update_state_calls.append({"delta": dict(delta), "as_node": as_node})


def _install(app, sid, *, status="running", closed=False, graph=None,
             recorder=None):
    from consultants.server.app import SessionState
    s = SessionState(sid=sid, cwd="/tmp", question="Q", effort="medium",
                     topology="council", status=status, closed=closed)
    if graph is not None:
        s._compiled = graph
        s._thread_config = {"configurable": {"thread_id": sid}}
    s._recorder = recorder
    app.state.sessions[sid] = s
    return s


def _client():
    from consultants.server.app import create_app
    app = create_app(run_council=None, start_reaper=False)
    return TestClient(app), app


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class TestInjectContract(unittest.TestCase):

    def test_research_phase_applies_in_place(self):
        c, app = _client()
        g = _FakeGraph(next_nodes=["researcher"])
        _install(app, "csl-r", graph=g)
        r = c.post("/v1/consult/csl-r/inject",
                   json={"role": "any", "text": "check the cache layer"})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual(b["status"], INJECT_STATUS_APPLIED)
        self.assertEqual(b["phase_at_apply"], PHASE_RESEARCH)
        self.assertEqual(b["target_role"], "researcher")
        self.assertEqual(b["routed"], ROUTED_IN_PLACE)
        self.assertEqual(len(g.update_state_calls), 1)

    def test_planning_phase_targets_planner(self):
        c, app = _client()
        g = _FakeGraph(next_nodes=["planner"])
        _install(app, "csl-p", graph=g)
        r = c.post("/v1/consult/csl-p/inject",
                   json={"role": "any", "text": "keep the plan to 3 items"})
        b = r.json()
        self.assertEqual(b["status"], INJECT_STATUS_APPLIED)
        self.assertEqual(b["phase_at_apply"], PHASE_PLANNING)
        self.assertEqual(b["target_role"], "planner")

    def test_synthesis_phase_honest_in_place_not_rewound(self):
        # Safe-layer: synthesis-phase inject is applied in place and
        # reports target_role=researcher (intent) but routed=in_place —
        # NOT rewound_to_researcher (that's the gated follow-up).
        c, app = _client()
        g = _FakeGraph(next_nodes=["synthesizer"])
        _install(app, "csl-s", graph=g)
        r = c.post("/v1/consult/csl-s/inject",
                   json={"role": "any", "text": "validate the claim at x:42"})
        b = r.json()
        self.assertEqual(b["status"], INJECT_STATUS_APPLIED)
        self.assertEqual(b["phase_at_apply"], PHASE_SYNTHESIS)
        self.assertEqual(b["target_role"], "researcher")
        self.assertEqual(b["routed"], ROUTED_IN_PLACE)

    def test_explicit_role_override(self):
        c, app = _client()
        g = _FakeGraph(next_nodes=["researcher"])
        _install(app, "csl-o", graph=g)
        r = c.post("/v1/consult/csl-o/inject",
                   json={"role": "synthesizer", "text": "lead with the verdict"})
        b = r.json()
        self.assertEqual(b["status"], INJECT_STATUS_APPLIED)
        # Explicit role wins over the phase default.
        self.assertEqual(b["target_role"], "synthesizer")

    def test_terminal_rejected(self):
        c, app = _client()
        g = _FakeGraph(next_nodes=["synthesizer"])
        _install(app, "csl-done", status="completed", graph=g)
        r = c.post("/v1/consult/csl-done/inject",
                   json={"role": "any", "text": "too late"})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual(b["status"], INJECT_STATUS_REJECTED)
        self.assertEqual(b["phase_at_apply"], PHASE_TERMINAL)
        self.assertIn("reason", b)
        self.assertEqual(g.update_state_calls, [])

    def test_pending_then_drain_on_state(self):
        c, app = _client()
        # No graph attached → pending.
        s = _install(app, "csl-race")
        r = c.post("/v1/consult/csl-race/inject",
                   json={"role": "any", "text": "x"})
        b = r.json()
        self.assertEqual(b["status"], INJECT_STATUS_PENDING)
        self.assertEqual(b["queue_position"], 1)
        # Runner attaches the graph; GET /state drains opportunistically.
        g = _FakeGraph(next_nodes=["researcher"])
        s._compiled = g
        s._thread_config = {"configurable": {"thread_id": "csl-race"}}
        rs = c.get("/v1/consult/csl-race/state")
        self.assertEqual(rs.status_code, 200)
        injs = rs.json()["injections"]
        self.assertEqual(len(injs), 1)
        self.assertEqual(injs[0]["status"], INJECT_STATUS_APPLIED)
        self.assertEqual(len(g.update_state_calls), 1)

    def test_failed_when_update_state_raises(self):
        c, app = _client()
        g = _FakeGraph(next_nodes=["researcher"])
        g.raise_on_update = True
        _install(app, "csl-f", graph=g)
        r = c.post("/v1/consult/csl-f/inject",
                   json={"role": "any", "text": "x"})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual(b["status"], INJECT_STATUS_FAILED)
        self.assertIn("error", b)

    def test_bad_payload_is_400(self):
        c, app = _client()
        g = _FakeGraph(next_nodes=["researcher"])
        _install(app, "csl-bad", graph=g)
        self.assertEqual(
            c.post("/v1/consult/csl-bad/inject",
                   json={"role": "nonsense", "text": "x"}).status_code, 400)
        self.assertEqual(
            c.post("/v1/consult/csl-bad/inject",
                   json={"role": "any", "text": ""}).status_code, 400)

    def test_idempotent_retry(self):
        c, app = _client()
        g = _FakeGraph(next_nodes=["researcher"])
        _install(app, "csl-i", graph=g)
        payload = {"role": "any", "text": "dedupe me"}
        first = c.post("/v1/consult/csl-i/inject", json=payload).json()
        second = c.post("/v1/consult/csl-i/inject", json=payload).json()
        self.assertEqual(first["injection_id"], second["injection_id"])
        # Only one real update_state despite two POSTs.
        self.assertEqual(len(g.update_state_calls), 1)

    def test_state_lists_injections(self):
        c, app = _client()
        g = _FakeGraph(next_nodes=["researcher"])
        _install(app, "csl-l", graph=g)
        c.post("/v1/consult/csl-l/inject",
               json={"role": "any", "text": "one"})
        body = c.get("/v1/consult/csl-l/state").json()
        self.assertIn("injections", body)
        self.assertEqual(len(body["injections"]), 1)
        self.assertEqual(body["injections"][0]["status"], INJECT_STATUS_APPLIED)


# ============================================================== #
# 4. SSE terminal complete frame
# ============================================================== #

class _RecorderRows:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def list_runtime_events(self, *, since_event_id=0, limit=1000):
        return [r for r in self.rows if int(r["event_id"]) > since_event_id]


class _Req:
    async def is_disconnected(self):
        return False


class TestSSEComplete(unittest.IsolatedAsyncioTestCase):

    async def test_terminal_emits_complete_event(self):
        from consultants.server.app import SessionState
        from consultants.server import control_routes as cr
        s = SessionState(sid="csl-sse", cwd="/x", question="Q",
                         effort="medium", topology="council",
                         status="completed")
        s.final_answer = "the answer"
        rec = _RecorderRows([
            {"event_id": 1, "ts": 1.0, "kind": "node_exit",
             "role": "synthesizer", "round": 1, "lane_idx": None,
             "payload": {"kind": "node_exit"}},
        ])
        frames = []
        async for chunk in cr._sse_stream_for_session(
                s, rec, _Req(), start_event_id=0,
                poll_interval_s=0.0, heartbeat_s=999.0):
            frames.append(chunk.decode("utf-8"))
        joined = "".join(frames)
        self.assertIn("event: complete", joined)
        self.assertIn('"status": "completed"', joined)
        self.assertIn('"final_answer_present": true', joined)


# ============================================================== #
# 5. Regression guard — follow-up checkpointer
# ============================================================== #

class TestFollowUpCheckpointerRegression(unittest.TestCase):
    """run_follow_up MUST pass checkpointer= to build_follow_up_graph.

    Without it, LangGraph get_state/update_state raise
    ValueError("No checkpointer set") and /inject + /state 500 on every
    follow-up sid. Source-inspection guard (cf. tests/test_popen_helpers.py)
    so the regression is caught without standing up a live graph.
    """

    def test_follow_up_passes_checkpointer(self):
        from consultants.server import runner
        src = inspect.getsource(runner)
        # The build_follow_up_graph call must carry checkpointer=.
        m = re.search(
            r"build_follow_up_graph\((.*?)\)", src, re.DOTALL,
        )
        self.assertIsNotNone(m, "build_follow_up_graph call not found")
        self.assertIn("checkpointer", m.group(1),
                      "run_follow_up must pass checkpointer= "
                      "(the #214/M9 follow-up 500 regression)")

    @unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
    def test_real_follow_up_graph_exposes_checkpointer(self):
        # Stronger than the source guard: build the REAL follow-up graph
        # with a real MemorySaver and confirm get_state / update_state
        # don't raise ValueError("No checkpointer set") — the exact
        # failure that 500'd /state + /inject on every follow-up. No
        # model call (get_state runs no nodes).
        from consultants.engine.graph import GraphDeps, build_follow_up_graph
        from consultants.engine.state_v2 import Doc

        class _Stub:
            def chat(self, payload, *, think=True):
                return {"choices": [{"message": {"role": "assistant",
                                                 "content": "x"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        deps = GraphDeps(
            chat_clients={"researcher": _Stub(), "synthesizer": _Stub()},
            models={"researcher": "m", "synthesizer": "m"},
            enabled_roles=("researcher", "synthesizer"),
            cwd="/tmp",
        )
        compiled = build_follow_up_graph(deps, checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "regr-fu"}}
        # Both calls raised pre-fix; must not now.
        snap = compiled.get_state(cfg)
        self.assertIsNotNone(snap)
        compiled.update_state(
            cfg, {"additional_context": [Doc(role="researcher", text="hi")]},
            as_node="researcher",
        )
        snap2 = compiled.get_state(cfg)
        ac = (getattr(snap2, "values", {}) or {}).get("additional_context") or []
        self.assertEqual(len(ac), 1)


if __name__ == "__main__":
    unittest.main()
