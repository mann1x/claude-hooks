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
import operator
import re
import unittest
from dataclasses import dataclass
from typing import Annotated, Optional, TypedDict


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

    def test_synthesis_phase_rewinds_to_researcher(self):
        # #314: a synthesis-phase inject (parked at the synthesizer
        # interrupt) targeting the researcher, with round budget, sets
        # the revalidation flag and reports rewound_to_researcher. The
        # Doc is written with NO as_node so the parked `next` is
        # preserved for the runner to drive the rewind.
        from consultants.server.control import ROUTED_REWOUND_TO_RESEARCHER
        c, app = _client()
        g = _FakeGraph(next_nodes=["synthesizer"])  # rounds_used=1, max=3
        s = _install(app, "csl-s", graph=g)
        r = c.post("/v1/consult/csl-s/inject",
                   json={"role": "any", "text": "validate the claim at x:42"})
        b = r.json()
        self.assertEqual(b["status"], INJECT_STATUS_APPLIED)
        self.assertEqual(b["phase_at_apply"], PHASE_SYNTHESIS)
        self.assertEqual(b["target_role"], "researcher")
        self.assertEqual(b["routed"], ROUTED_REWOUND_TO_RESEARCHER)
        # The runner-facing revalidation flag is set (take clears it).
        self.assertTrue(s.take_revalidation())
        # Doc written without disturbing the parked `next` (no as_node).
        self.assertEqual(len(g.update_state_calls), 1)
        self.assertIsNone(g.update_state_calls[0]["as_node"])

    def test_synthesis_phase_best_effort_when_caps_exhausted(self):
        # Round budget exhausted (rounds_used >= max_rounds) → no
        # rewind; the Doc still reaches the synthesizer in place and the
        # inject reports best_effort_cap_reached.
        from consultants.server.control import ROUTED_BEST_EFFORT_CAP_REACHED
        c, app = _client()
        g = _FakeGraph(next_nodes=["synthesizer"], values={
            "runtime_control": {"max_rounds": 1},
            "research_rounds_used": 1, "critic_reroutes_used": 0,
            "effort": "medium", "additional_context": [], "confidence": [],
            "final_answer": "", "error": None,
        })
        s = _install(app, "csl-cap", graph=g)
        r = c.post("/v1/consult/csl-cap/inject",
                   json={"role": "any", "text": "late validate"})
        b = r.json()
        self.assertEqual(b["status"], INJECT_STATUS_APPLIED)
        self.assertEqual(b["routed"], ROUTED_BEST_EFFORT_CAP_REACHED)
        self.assertFalse(s.take_revalidation())  # no rewind requested

    def _checkpoint_graph(self):
        return _FakeGraph(next_nodes=["synthesizer"], values={
            "runtime_control": {"max_rounds": 1},
            "research_rounds_used": 1, "critic_reroutes_used": 0,
            "effort": "xhigh", "additional_context": [], "confidence": [],
            "final_answer": "", "error": None,
        })

    @staticmethod
    def _docs(g):
        return [d for c in g.update_state_calls
                for d in c["delta"].get("additional_context") or []]

    def test_a_critic_brief_rewinds_and_reaches_the_synthesizer(self):
        """csl-2026-10-18-1033-5a67: a critic brief at the checkpoint
        said ``applied / in_place`` and reached no prompt — the critic
        never ran again and the synthesizer reads only its own docs."""
        from consultants.server.control import ROUTED_REWOUND_TO_RESEARCHER
        c, app = _client()
        g = _FakeGraph(next_nodes=["synthesizer"])      # budget left
        s = _install(app, "csl-crit", graph=g)
        b = c.post("/v1/consult/csl-crit/inject",
                   json={"role": "critic", "text": "tests were not run"}).json()
        self.assertEqual(b["routed"], ROUTED_REWOUND_TO_RESEARCHER)
        self.assertTrue(s.take_revalidation())
        roles = sorted(d.role for d in self._docs(g))
        self.assertEqual(roles, ["critic", "synthesizer"])
        syn = [d for d in self._docs(g) if d.role == "synthesizer"][0]
        self.assertIn("addressed to the critic", syn.text)
        self.assertIn("tests were not run", syn.text)

    def test_a_checkpoint_brief_buys_one_extra_round_once(self):
        from consultants.server.control import (ROUTED_BEST_EFFORT_CAP_REACHED,
                                                ROUTED_REWOUND_EXTRA_ROUND)
        c, app = _client()
        g = self._checkpoint_graph()                    # cap spent
        s = _install(app, "csl-ck", graph=g)
        s._adversary_checkpoint_active = True
        b = c.post("/v1/consult/csl-ck/inject",
                   json={"role": "researcher", "text": "go look at apply-patch"}).json()
        self.assertEqual(b["routed"], ROUTED_REWOUND_EXTRA_ROUND)
        self.assertEqual(g.update_state_calls[0]["delta"]["runtime_control"],
                         {"max_rounds": 2})
        self.assertTrue(s.take_revalidation())
        b2 = c.post("/v1/consult/csl-ck/inject",
                    json={"role": "critic", "text": "and the locks"}).json()
        self.assertEqual(b2["routed"], ROUTED_BEST_EFFORT_CAP_REACHED)
        self.assertNotIn("runtime_control", g.update_state_calls[1]["delta"])
        self.assertFalse(s.take_revalidation())
        # the second brief still reaches the synthesizer
        self.assertIn("synthesizer", [d.role for d in
                                      g.update_state_calls[1]["delta"]["additional_context"]])

    def test_no_extra_round_outside_the_checkpoint(self):
        from consultants.server.control import ROUTED_BEST_EFFORT_CAP_REACHED
        c, app = _client()
        g = self._checkpoint_graph()
        s = _install(app, "csl-nock", graph=g)
        b = c.post("/v1/consult/csl-nock/inject",
                   json={"role": "researcher", "text": "late"}).json()
        self.assertEqual(b["routed"], ROUTED_BEST_EFFORT_CAP_REACHED)
        self.assertFalse(s.take_revalidation())
        self.assertEqual(sorted(d.role for d in self._docs(g)),
                         ["researcher", "synthesizer"])

    def test_a_synthesizer_brief_is_not_copied(self):
        c, app = _client()
        g = _FakeGraph(next_nodes=["synthesizer"])
        _install(app, "csl-syn", graph=g)
        b = c.post("/v1/consult/csl-syn/inject",
                   json={"role": "synthesizer", "text": "lead with it"}).json()
        self.assertEqual(b["routed"], ROUTED_IN_PLACE)
        self.assertEqual([d.role for d in self._docs(g)], ["synthesizer"])

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


class TestSSEReplay(unittest.IsolatedAsyncioTestCase):
    """csl-2026-10-18-1033-5a67: ``events --milestones`` showed no role on
    any line, and a finished council replayed nothing but ``complete``."""

    async def _frames(self, s, rec):
        from consultants.server import control_routes as cr
        out = []
        async for chunk in cr._sse_stream_for_session(
                s, rec, _Req(), start_event_id=0,
                poll_interval_s=0.0, heartbeat_s=999.0):
            out.append(chunk.decode("utf-8"))
        return "".join(out)

    def _session(self, cwd):
        from consultants.server.app import SessionState
        return SessionState(sid="csl-rp", cwd=str(cwd), question="Q",
                            effort="medium", topology="council",
                            status="completed")

    async def test_events_carry_role_and_lane(self):
        rec = _RecorderRows([{"event_id": 1, "ts": 1.0, "kind": "node_enter",
                              "role": "researcher", "round": 1, "lane_idx": 3,
                              "payload": {"kind": "node_enter"}}])
        joined = await self._frames(self._session("/x"), rec)
        self.assertIn('"role": "researcher"', joined)
        self.assertIn('"lane_idx": 3', joined)

    async def test_a_finished_council_replays_from_its_transcript(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / ".claude-hooks/consultants/csl-rp/transcript.db"
            db.parent.mkdir(parents=True)
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE runtime_events (event_id INTEGER PRIMARY KEY, "
                        "ts REAL, kind TEXT, role TEXT, round INTEGER, "
                        "lane_idx INTEGER, payload TEXT)")
            con.execute("INSERT INTO runtime_events VALUES (1, 1.0, 'node_exit', "
                        "'critic', 1, NULL, '{\"kind\": \"node_exit\"}')")
            con.commit()
            con.close()

            class Closed:
                _closed = True

                def list_runtime_events(self, **kw):
                    return []
            joined = await self._frames(self._session(d), Closed())
        self.assertIn("event: node_exit", joined)
        self.assertIn('"role": "critic"', joined)
        self.assertIn("event: complete", joined)


class TestStateShowsTheReview(unittest.TestCase):
    """At the checkpoint ``state`` showed counts only; the reviewer had to
    read transcript.db to see what they were judging."""

    def test_previews_and_critique(self):
        from consultants.server.control import summarize_state_for_get
        body = summarize_state_for_get({"values": {
            "research": ["short", "x" * 1000], "critique": "DECISION: ready"}},
            sid="s")
        self.assertEqual(body["research_previews"][0], "short")
        self.assertIn("… [preview: ", body["research_previews"][1])
        self.assertLess(len(body["research_previews"][1]), 400)
        self.assertEqual(body["critique"], "DECISION: ready")
        self.assertEqual(body["research_count"], 2)


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


# ============================================================== #
# 6. Checkpointer msgpack serde allowlist
# ============================================================== #

@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestCheckpointerSerdeAllowlist(unittest.TestCase):
    """The M9 checkpointer round-trips custom CouncilState channel
    dataclasses through LangGraph's msgpack serde. Under the coming
    strict mode, unregistered types are SILENTLY degraded back to
    plain dicts on read — which would make downstream doc.role /
    doc.text / turn.role accesses AttributeError mid-graph. The serde
    allowlist (make_checkpointer_serde) must keep every such type a
    real instance across the round-trip.
    """

    def _samples(self):
        from consultants.engine.state_v2 import (
            Doc, ToolPlanItem, ToolResult, InterruptState,
            CoderTaskItem, CoderArtifact,
        )
        from consultants.engine.storage import RoleTurn
        return [
            Doc(role="researcher", text="t"),
            ToolPlanItem(intent="find X"),
            ToolResult(intent="find X", content="r"),
            InterruptState(kind="k", prompt="p", posted_at=1.0),
            CoderTaskItem(task="write foo"),
            CoderArtifact(task="write foo", summary="done"),
            RoleTurn(role="researcher", round=1, content="c"),
        ]

    def test_all_custom_types_round_trip_under_strict_serde(self):
        from consultants.engine.state_v2 import make_checkpointer_serde
        serde = make_checkpointer_serde()
        self.assertIsNotNone(serde)
        for obj in self._samples():
            enc = serde.dumps_typed({"v": [obj]})
            out = serde.loads_typed(enc)["v"][0]
            self.assertIs(
                type(out), type(obj),
                msg=f"{type(obj).__name__} degraded to {type(out).__name__} "
                    "across checkpoint round-trip — add it to "
                    "checkpointer_msgpack_types()",
            )

    def test_allowlist_covers_graph_schema_dataclass_channels(self):
        # Drift guard: introspect graph.CouncilState's channel
        # annotations, collect every dataclass element type, and assert
        # each is in the serde allowlist. Catches a future engineer
        # adding a new custom-typed channel without allowlisting it.
        import dataclasses
        import typing
        from consultants.engine import graph as graph_mod
        from consultants.engine.state_v2 import checkpointer_msgpack_types

        allow = set(checkpointer_msgpack_types())

        def _collect(tp, acc):
            if dataclasses.is_dataclass(tp) and isinstance(tp, type):
                acc.add(tp)
            for arg in typing.get_args(tp):
                _collect(arg, acc)

        found: set = set()
        for ann in graph_mod.CouncilState.__annotations__.values():
            _collect(ann, found)

        missing = found - allow
        self.assertEqual(
            missing, set(),
            msg=f"CouncilState channels reference dataclass types not in "
                f"the checkpointer serde allowlist: "
                f"{sorted(t.__name__ for t in missing)}",
        )


# ============================================================== #
# 7. Runner drive-loop: auto-resume (parity) / rewind / HITL park
# ============================================================== #

# Module-level so langgraph's get_type_hints resolves the (stringified
# under `from __future__ import annotations`) channel annotations
# against this module's globals (Annotated / operator / TypedDict).
class _DriveSt(TypedDict, total=False):
    log: Annotated[list, operator.add]
    additional_context: Annotated[list, operator.add]
    research_rounds_used: Annotated[int, operator.add]
    critic_decision: Optional[str]
    effort: str
    runtime_control: dict


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestDriveCouncilStream(unittest.TestCase):
    """#314: drive _drive_council_stream against a REAL langgraph graph
    compiled with interrupt_before=[synthesizer], mirroring the council
    shape. Stub graphs can't exercise the interrupt, so this is the
    coverage that proves the always-on interrupt + resume loop."""

    def _graph(self):
        from langgraph.graph import StateGraph, START, END
        from langgraph.checkpoint.memory import MemorySaver

        St = _DriveSt

        def researcher(s): return {"log": ["R"], "research_rounds_used": 1}
        def critic(s): return {"log": ["C"], "critic_decision": "ready"}
        def synthesizer(s): return {"log": ["S"]}
        def route(s): return "synthesizer"

        g = StateGraph(St)
        g.add_node("researcher", researcher)
        g.add_node("critic", critic)
        g.add_node("synthesizer", synthesizer)
        g.add_edge(START, "researcher")
        g.add_edge("researcher", "critic")
        g.add_conditional_edges("critic", route,
                                {"researcher": "researcher",
                                 "synthesizer": "synthesizer"})
        g.add_edge("synthesizer", END)
        return g.compile(checkpointer=MemorySaver(),
                         interrupt_before=["synthesizer"])

    def _state(self):
        from consultants.server.app import SessionState
        s = SessionState(sid="drive", cwd="/x", question="q",
                         effort="medium", topology="council")
        s.progress = {"researcher": "in_progress", "critic": "pending",
                      "synthesizer": "pending"}
        return s

    def _drive(self, *, revalidate=False, review=False):
        from consultants.server.runner import _drive_council_stream
        compiled = self._graph()
        s = self._state()
        cfg = {"configurable": {"thread_id": s.sid}}
        # max_rounds=3 gives the rewind budget (medium's effort cap is 1,
        # under which a synthesis-phase inject would be best-effort).
        initial = {"log": [], "research_rounds_used": 0, "effort": "medium",
                   "runtime_control": {"max_rounds": 3}}
        if revalidate:
            s.request_revalidation()
        final = _drive_council_stream(
            compiled, initial, cfg, state=s, enabled=("researcher",
            "critic", "synthesizer"), review_before_synthesis=review,
            recorder=None, log_label="test")
        return final, compiled, cfg

    def test_auto_resume_runs_synthesizer_parity(self):
        # Default path: no inject → auto-resume past the interrupt →
        # synthesizer runs. Byte-identical to a non-interrupt run.
        final, _c, _cfg = self._drive()
        self.assertEqual(final.get("log"), ["R", "C", "S"])

    def test_rewind_runs_extra_researcher_round(self):
        # revalidation pending + budget → one extra researcher(+critic)
        # pass before the synthesizer. take_revalidation is one-shot so
        # it does NOT loop forever.
        final, _c, _cfg = self._drive(revalidate=True)
        self.assertEqual(final.get("log"), ["R", "C", "R", "C", "S"])
        self.assertEqual(final.get("research_rounds_used"), 2)

    def test_a_cap_raised_during_the_checkpoint_rewinds(self):
        """The extra round a checkpoint brief buys must be seen by the
        runner: it re-reads the state after the pause rather than using
        the snapshot from before it."""
        from consultants.server.control_routes import _make_inject_apply_fn
        from consultants.server.control import Injection
        from consultants.server.runner import _drive_council_stream
        compiled = self._graph()
        s = self._state()
        cfg = {"configurable": {"thread_id": s.sid}}
        s._compiled, s._thread_config = compiled, cfg
        apply_fn = _make_inject_apply_fn(s)
        sent = []

        def during_pause(_secs):
            if not sent:
                inj = Injection(id="i1", role="researcher", text="go look")
                apply_fn(inj)
                sent.append(inj)
                s.ack_adversary()
        initial = {"log": [], "research_rounds_used": 0, "effort": "medium",
                   "runtime_control": {"max_rounds": 1}}
        final = _drive_council_stream(
            compiled, initial, cfg, state=s, enabled=("researcher", "critic",
            "synthesizer"), review_before_synthesis=False, recorder=None,
            log_label="test", adversary_checkpoint=True,
            adversary_checkpoint_timeout_s=60, sleep_fn=during_pause)
        self.assertEqual(sent[0].routed, "rewound_extra_round")
        self.assertEqual(final.get("log"), ["R", "C", "R", "C", "S"])

    def test_review_before_synthesis_parks(self):
        # HITL: parks before the synthesizer (synthesizer does NOT run),
        # leaving the graph for an external /resume — M5 behavior.
        final, compiled, cfg = self._drive(review=True)
        self.assertEqual(final.get("log"), ["R", "C"])
        self.assertIn("synthesizer", tuple(compiled.get_state(cfg).next or ()))


if __name__ == "__main__":
    unittest.main()


# ============================================================== #
# 8. #1626: a checkpoint rewind on the REAL council graph
# ============================================================== #
@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestCheckpointRewindOnTheCouncilGraph(unittest.TestCase):
    """csl-2026-10-18-1120-95ce: a researcher brief at the adversary
    checkpoint said ``applied, rewound_extra_round``, then the run went
    back to the PLANNER, all lanes restarted as round 1, and the brief
    was in 0 of 226 prompts. The toy graphs above have no planner, so
    they could not see it."""

    BRIEF = "ADVERSARY BRIEF: grep re-stamps without changedSince"

    def _run(self, *, extras=("m2",)):
        from langgraph.checkpoint.memory import MemorySaver
        from consultants.engine.graph import GraphDeps, build_council_graph
        from consultants.server.app import SessionState
        from consultants.server.control import Injection
        from consultants.server.control_routes import _make_inject_apply_fn
        from consultants.server.runner import _drive_council_stream

        calls: dict[str, list] = {"planner": [], "researcher": [],
                                  "critic": [], "synthesizer": []}

        def _text(payload):
            return "\n".join(str(m.get("content") or "")
                             for m in payload.get("messages") or [])

        class _Role:
            def __init__(self, role, reply):
                self.role, self.reply = role, reply

            def chat(self, payload, *, think=True, **_kw):
                calls[self.role].append(
                    (payload.get("model"), _text(payload)))
                n = len(calls[self.role])
                return {"choices": [{"message": {
                    "role": "assistant",
                    "content": self.reply(n)}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        deps = GraphDeps(
            chat_clients={
                "planner": _Role("planner", lambda n:
                                 "1. read the grep tool\n2. read apply_patch"),
                "researcher": _Role("researcher",
                                    lambda n: f"REPORT-{n}: findings"),
                "critic": _Role("critic", lambda n: "DECISION: ready"),
                "synthesizer": _Role("synthesizer",
                                     lambda n: "FINAL ANSWER"),
            },
            models={r: "m1" for r in ("planner", "researcher", "critic",
                                      "synthesizer")},
            enabled_roles=("planner", "researcher", "critic", "synthesizer"),
            extra_models_by_role={"researcher": list(extras)},
            cwd="/tmp", tool_executor=lambda *a, **kw: "",
            tool_specs=[], grounding_msgs=[], disable_cache=True,
        )
        compiled = build_council_graph(deps, checkpointer=MemorySaver(),
                                       interrupt_before=["synthesizer"])
        s = SessionState(sid="rw", cwd="/tmp", question="audit the tools",
                         effort="high", topology="council")
        s.progress = {r: "pending" for r in calls}
        cfg = {"configurable": {"thread_id": s.sid}}
        s._compiled, s._thread_config = compiled, cfg
        apply_fn = _make_inject_apply_fn(s)
        sent, before = [], {}

        def during_pause(_secs):
            if not sent:
                before.update({k: len(v) for k, v in calls.items()})
                inj = Injection(id="i1", role="researcher", text=self.BRIEF,
                                source="adversary")
                apply_fn(inj)
                sent.append(inj)
                s.ack_adversary()
        initial = {"question": "audit the tools", "cwd": "/tmp",
                   "models": deps.models, "topology": "council",
                   "effort": "high", "research_rounds_used": 0,
                   "runtime_control": {"max_rounds": 1}}
        final = _drive_council_stream(
            compiled, initial, cfg, state=s,
            enabled=("planner", "researcher", "critic", "synthesizer"),
            review_before_synthesis=False, recorder=None, log_label="t",
            adversary_checkpoint=True, adversary_checkpoint_timeout_s=60,
            sleep_fn=during_pause)
        return final, calls, before, sent[0]

    def test_the_rewind_continues_research_with_the_brief(self):
        final, calls, before, inj = self._run()
        self.assertEqual(inj.routed, "rewound_extra_round")
        # Never back to the planner.
        self.assertEqual(len(calls["planner"]), before["planner"])
        after = calls["researcher"][before["researcher"]:]
        # One lane per council model, each with the brief and the
        # reports of the first round.
        self.assertEqual(sorted(m for m, _ in after), ["m1", "m2"])
        for _m, text in after:
            self.assertIn(self.BRIEF, text)
            self.assertIn("REPORT-1", text)
        # The first round never saw a brief (it did not exist yet).
        for _m, text in calls["researcher"][:before["researcher"]]:
            self.assertNotIn(self.BRIEF, text)

    def test_the_critic_runs_on_the_new_reports_and_reads_the_brief(self):
        final, calls, before, _inj = self._run()
        after = calls["critic"][before["critic"]:]
        self.assertEqual(len(after), 1, "no critic pass after the rewind")
        self.assertIn(self.BRIEF, after[0][1])
        self.assertIn("verified", after[0][1])
        self.assertIn("FINAL ANSWER", final.get("final_answer", ""))
        self.assertIn(self.BRIEF, calls["synthesizer"][-1][1])

    def test_a_single_model_council_rewinds_as_one_full_state_pass(self):
        _final, calls, before, _inj = self._run(extras=())
        self.assertEqual(len(calls["planner"]), before["planner"])
        after = calls["researcher"][before["researcher"]:]
        self.assertEqual(len(after), 1)
        self.assertIn(self.BRIEF, after[0][1])


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestProgressFollowsTheLanes(unittest.TestCase):
    """mail #1625: status.progress said researcher=done, critic=in_progress
    while 6 of 8 lanes were still making calls."""

    def test_a_role_is_done_when_its_last_lane_ends(self):
        import time as _t
        from langgraph.graph import StateGraph, START, END
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.types import Send
        from consultants.server.app import SessionState
        from consultants.server.runner import _drive_council_stream

        s = SessionState(sid="prog", cwd="/x", question="q",
                         effort="medium", topology="council")
        s.progress = {"planner": "in_progress", "researcher": "pending",
                      "critic": "pending", "synthesizer": "pending"}
        seen = []

        def researcher(st):
            _t.sleep(0.05 * (st["i"] + 1))
            if st["i"] == 2:        # the slowest lane, after two have ended
                seen.append(dict(s.progress))
            return {"log": [f"R{st['i']}"]}

        g = StateGraph(_DriveSt)
        g.add_node("planner", lambda st: {"log": ["P"]})
        g.add_node("researcher", researcher)
        g.add_node("critic", lambda st: {"log": ["C"]})
        g.add_node("synthesizer", lambda st: {"log": ["S"]})
        g.add_edge(START, "planner")
        g.add_conditional_edges("planner", lambda st: [
            Send("researcher", {"i": i}) for i in range(3)], ["researcher"])
        g.add_edge("researcher", "critic")
        g.add_edge("critic", "synthesizer")
        g.add_edge("synthesizer", END)
        compiled = g.compile(checkpointer=MemorySaver(),
                             interrupt_before=["synthesizer"])
        _drive_council_stream(
            compiled, {"log": []}, {"configurable": {"thread_id": "prog"}},
            state=s, enabled=("planner", "researcher", "critic",
                              "synthesizer"),
            review_before_synthesis=False, recorder=None, log_label="t")
        self.assertEqual(seen[0]["researcher"], "in_progress")
        self.assertEqual(seen[0]["critic"], "pending")
        self.assertEqual(seen[0]["planner"], "done")
        self.assertEqual(set(s.progress.values()), {"done"})
