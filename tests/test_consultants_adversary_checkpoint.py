"""M2 — adversary checkpoint: the engine-initiated pause-before-synthesis.

When the opt-in ``adversary_checkpoint`` knob is enabled, the runner parks
ONCE at the always-on synthesizer interrupt, emits/records an
``awaiting_adversary`` event, and blocks (as the sole resumer) until the
consumer acks OR the deadline passes — then resumes (honoring any brief
injected during the pause via the existing rewind path). Default OFF keeps
the council byte-identical (cohort-2 parity lives in
test_consultants_v2_parity.py).

Cohorts here:
- ``_await_adversary_checkpoint`` unit (mocked clock): ack-before-deadline,
  deadline-no-ack, deadline_ts set-while-parked + cleared-after, event
  recorded with deadline_ts.
- ``_drive_council_stream`` against a REAL langgraph graph: enabled+ack →
  resume; enabled+no-ack → auto-resume at deadline; enabled+brief-during-pause
  → rewind (x-tier path); fire-once (one event even across a rewind);
  disabled → identical to auto-resume + zero events.
- SessionState ack signal read-and-clear semantics.
- POST /adversary-ack endpoint: sets the flag, never re-invokes the graph,
  surfaces checkpoint_open; 404/409 guards.
- Real MessageRecorder round-trips an awaiting_adversary row (Last-Event-ID
  replay).
"""

from __future__ import annotations

import operator
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Optional, TypedDict

try:
    # Concrete-leaf probe — a bare ``import langgraph`` is fooled by the empty
    # ghost namespace dirs a pip-uninstall leaves behind. See the note in
    # test_consultants_adversary_role.py.
    from langgraph.graph import StateGraph  # noqa: F401
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

from consultants.server import runner as runner_mod


# ----------------------- fakes ----------------------------------- #

class _FakeRecorder:
    """Captures record_event calls for assertion."""

    def __init__(self):
        self.events: list[dict] = []

    def record_event(self, *, kind, role=None, round=None,
                     lane_idx=None, payload=None):
        self.events.append({"kind": kind, "role": role, "round": round,
                            "lane_idx": lane_idx, "payload": dict(payload or {})})

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


def _session(sid: str = "csl-adv"):
    from consultants.server.app import SessionState
    s = SessionState(sid=sid, cwd="/x", question="q",
                     effort="high", topology="council", status="running")
    s.progress = {"researcher": "in_progress", "critic": "pending",
                  "synthesizer": "pending"}
    return s


# ============================================================== #
# 1. _await_adversary_checkpoint — mocked clock
# ============================================================== #

class TestAwaitAdversaryCheckpoint(unittest.TestCase):

    def test_ack_before_deadline_returns_true(self):
        s = _session()
        rec = _FakeRecorder()
        clock = [1000.0]

        def now_fn():
            return clock[0]

        def sleep_fn(dt):
            # deliver the ack on the first park-poll sleep
            s.ack_adversary()
            clock[0] += dt

        acked = runner_mod._await_adversary_checkpoint(
            state=s, final_state={"confidence": [0.4]}, recorder=rec,
            timeout_s=600.0, log_label="t",
            now_fn=now_fn, sleep_fn=sleep_fn, poll_interval=0.5,
        )
        self.assertTrue(acked)
        # deadline cleared on exit
        self.assertIsNone(s._checkpoint_deadline_ts)
        # event recorded with a deadline + self-confidence from M0 channel
        self.assertEqual(rec.kinds(), ["awaiting_adversary"])
        payload = rec.events[0]["payload"]
        self.assertEqual(payload["deadline_ts"], 1000.0 + 600.0)
        self.assertEqual(payload["self_confidence"], 0.4)
        self.assertEqual(payload["timeout_s"], 600.0)

    def test_no_ack_returns_false_at_deadline(self):
        s = _session()
        rec = _FakeRecorder()
        clock = [1000.0]

        def now_fn():
            return clock[0]

        def sleep_fn(dt):
            clock[0] += dt  # never acks

        acked = runner_mod._await_adversary_checkpoint(
            state=s, final_state={}, recorder=rec,
            timeout_s=2.0, log_label="t",
            now_fn=now_fn, sleep_fn=sleep_fn, poll_interval=0.5,
        )
        self.assertFalse(acked)
        self.assertIsNone(s._checkpoint_deadline_ts)
        # clock advanced to or past the deadline
        self.assertGreaterEqual(clock[0], 1002.0)

    def test_deadline_ts_visible_while_parked(self):
        s = _session()
        rec = _FakeRecorder()
        clock = [500.0]
        seen: list = []

        def now_fn():
            return clock[0]

        def sleep_fn(dt):
            # observe the deadline mid-park, then ack to exit
            seen.append(s._checkpoint_deadline_ts)
            s.ack_adversary()
            clock[0] += dt

        runner_mod._await_adversary_checkpoint(
            state=s, final_state={}, recorder=rec, timeout_s=300.0,
            log_label="t", now_fn=now_fn, sleep_fn=sleep_fn,
        )
        # while parked, the deadline was the stamped wall-clock
        self.assertEqual(seen, [500.0 + 300.0])
        # cleared after
        self.assertIsNone(s._checkpoint_deadline_ts)

    def test_floor_timeout_at_one_second(self):
        s = _session()
        rec = _FakeRecorder()
        clock = [0.0]

        def now_fn():
            return clock[0]

        def sleep_fn(dt):
            clock[0] += dt

        # a sub-second / zero timeout is floored to 1s so the deadline is
        # always strictly in the future at entry.
        runner_mod._await_adversary_checkpoint(
            state=s, final_state={}, recorder=rec, timeout_s=0.0,
            log_label="t", now_fn=now_fn, sleep_fn=sleep_fn,
        )
        self.assertEqual(rec.events[0]["payload"]["deadline_ts"], 1.0)

    def test_pre_ack_releases_immediately(self):
        # An ack that arrives BEFORE the checkpoint opens must be honored
        # (the entry must NOT clear it) — the first poll consumes it and
        # releases without ever sleeping. (review finding #6)
        s = _session()
        rec = _FakeRecorder()
        s.ack_adversary()
        slept: list = []

        def now_fn():
            return 1000.0

        def sleep_fn(dt):
            slept.append(dt)

        acked = runner_mod._await_adversary_checkpoint(
            state=s, final_state={}, recorder=rec, timeout_s=600.0,
            log_label="t", now_fn=now_fn, sleep_fn=sleep_fn,
        )
        self.assertTrue(acked)
        self.assertEqual(slept, [])  # released on the first poll

    def test_closed_breaks_park_before_deadline(self):
        # A session closed mid-park (discard-cancel / idle reaper) must
        # release the runner thread promptly, not block the full timeout.
        s = _session()
        rec = _FakeRecorder()
        clock = [1000.0]

        def now_fn():
            return clock[0]

        def sleep_fn(dt):
            s.closed = True  # closed during the park
            clock[0] += dt

        acked = runner_mod._await_adversary_checkpoint(
            state=s, final_state={}, recorder=rec, timeout_s=600.0,
            log_label="t", now_fn=now_fn, sleep_fn=sleep_fn,
        )
        self.assertFalse(acked)            # released by close, not ack
        self.assertLess(clock[0], 1600.0)  # did not wait the full timeout


# ============================================================== #
# 2. _drive_council_stream with the checkpoint (real langgraph)
# ============================================================== #

class _DriveSt(TypedDict, total=False):
    log: Annotated[list, operator.add]
    research_rounds_used: Annotated[int, operator.add]
    critic_decision: Optional[str]
    effort: str
    runtime_control: dict


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestDriveCouncilStreamAdversaryCheckpoint(unittest.TestCase):
    """Mirror of TestDriveCouncilStream (#314) with the M2 checkpoint
    engaged. Real graph compiled interrupt_before=[synthesizer]."""

    def _graph(self):
        from langgraph.graph import StateGraph, START, END
        from langgraph.checkpoint.memory import MemorySaver

        St = _DriveSt

        def researcher(s):
            return {"log": ["R"], "research_rounds_used": 1}

        def critic(s):
            return {"log": ["C"], "critic_decision": "ready"}

        def synthesizer(s):
            return {"log": ["S"]}

        def route(s):
            return "synthesizer"

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

    def _drive(self, *, checkpoint, ack=False, revalidate_during=False,
               close_during=False, recorder=None, timeout_s=600.0):
        from consultants.server.runner import _drive_council_stream
        compiled = self._graph()
        s = _session("drive-adv")
        cfg = {"configurable": {"thread_id": s.sid}}
        initial = {"log": [], "research_rounds_used": 0, "effort": "high",
                   "runtime_control": {"max_rounds": 3}}

        # mocked clock: on the first park-poll sleep, optionally deliver an
        # inject-brief (request_revalidation), an ack, and/or a close.
        clock = [1000.0]

        def now_fn():
            return clock[0]

        def sleep_fn(dt):
            if close_during:
                s.closed = True
            if revalidate_during:
                s.request_revalidation()
            if ack or revalidate_during:
                s.ack_adversary()
            clock[0] += dt

        final = _drive_council_stream(
            compiled, initial, cfg, state=s,
            enabled=("researcher", "critic", "synthesizer"),
            review_before_synthesis=False, recorder=recorder,
            log_label="test", adversary_checkpoint=checkpoint,
            adversary_checkpoint_timeout_s=timeout_s,
            now_fn=now_fn, sleep_fn=sleep_fn,
        )
        return final, s

    def test_disabled_is_identical_to_auto_resume(self):
        rec = _FakeRecorder()
        final, _s = self._drive(checkpoint=False, recorder=rec)
        self.assertEqual(final.get("log"), ["R", "C", "S"])
        # no checkpoint event when disabled (parity)
        self.assertNotIn("awaiting_adversary", rec.kinds())

    def test_enabled_ack_resumes(self):
        rec = _FakeRecorder()
        final, s = self._drive(checkpoint=True, ack=True, recorder=rec)
        self.assertEqual(final.get("log"), ["R", "C", "S"])
        # exactly one checkpoint fired
        self.assertEqual(rec.kinds().count("awaiting_adversary"), 1)
        self.assertIsNone(s._checkpoint_deadline_ts)
        # sole-resumer guard disarmed once the runner returns (finally)
        self.assertFalse(s._adversary_checkpoint_active)

    def test_closed_during_pause_returns_without_synthesizing(self):
        # /cancel --discard (or idle reap) closes the session mid-park →
        # the runner returns early WITHOUT re-streaming the synthesizer,
        # and the sole-resumer guard is cleared.
        rec = _FakeRecorder()
        final, s = self._drive(checkpoint=True, close_during=True,
                               recorder=rec)
        self.assertEqual(final.get("log"), ["R", "C"])  # no "S"
        self.assertFalse(s._adversary_checkpoint_active)
        self.assertEqual(rec.kinds().count("awaiting_adversary"), 1)

    def test_enabled_no_ack_auto_resumes_at_deadline(self):
        rec = _FakeRecorder()
        # small timeout so the mocked clock crosses it quickly
        final, _s = self._drive(checkpoint=True, ack=False, recorder=rec,
                                timeout_s=2.0)
        self.assertEqual(final.get("log"), ["R", "C", "S"])
        self.assertEqual(rec.kinds().count("awaiting_adversary"), 1)

    def test_brief_during_pause_triggers_rewind(self):
        # A role=researcher brief injected during the pause sets
        # request_revalidation; on checkpoint exit the runner honors it via
        # the existing rewind path → one extra researcher+critic round.
        rec = _FakeRecorder()
        final, _s = self._drive(checkpoint=True, revalidate_during=True,
                                recorder=rec)
        self.assertEqual(final.get("log"), ["R", "C", "R", "C", "S"])
        self.assertEqual(final.get("research_rounds_used"), 2)
        # fire-once: the checkpoint does NOT re-fire when the rewind
        # re-parks at the synthesizer interrupt.
        self.assertEqual(rec.kinds().count("awaiting_adversary"), 1)


# ============================================================== #
# 3. SessionState ack signal
# ============================================================== #

class TestSessionStateAckSignal(unittest.TestCase):

    def test_ack_and_take_read_and_clear(self):
        s = _session()
        self.assertFalse(s.take_adversary_ack())  # default
        s.ack_adversary()
        self.assertTrue(s.take_adversary_ack())   # one-shot
        self.assertFalse(s.take_adversary_ack())  # cleared

    def test_public_dict_omits_deadline_when_idle(self):
        # Parity: the key is ABSENT on the default path (no open
        # checkpoint), so GET /state is byte-identical to pre-M2.
        s = _session()
        self.assertNotIn("adversary_checkpoint_deadline_ts", s.public_dict())

    def test_public_dict_surfaces_deadline_when_open(self):
        s = _session()
        s._checkpoint_deadline_ts = 1234.5
        self.assertEqual(
            s.public_dict()["adversary_checkpoint_deadline_ts"], 1234.5)


# ============================================================== #
# 4. POST /adversary-ack endpoint
# ============================================================== #

class _NoopGraph:
    """Minimal live-graph stub: update_state is a no-op (so control
    verbs that apply a delta succeed); invoke MUST NOT be called during a
    checkpoint (the sole-resumer invariant)."""

    def update_state(self, *a, **k):
        pass

    def invoke(self, *a, **k):  # pragma: no cover — guarded
        raise AssertionError("invoke must not be called during a checkpoint")

    def get_state(self, *a, **k):
        class _S:
            values: dict = {}
            next: tuple = ()
        return _S()


class _InvokeCounter:
    """Live-graph stub that counts invoke() calls (the double-resume
    signal). update_state is a no-op."""

    def __init__(self):
        self.invoke_calls = 0

    def invoke(self, *a, **k):
        self.invoke_calls += 1

    def update_state(self, *a, **k):
        pass


def _install_running_session(app, sid, *, status="running",
                             deadline=None):
    from consultants.server.app import SessionState
    s = SessionState(sid=sid, cwd="/tmp", question="Q", effort="high",
                     topology="council", status=status)
    # a live graph handle is required by _require_live_session
    s._compiled = _NoopGraph()
    s._thread_config = {"configurable": {"thread_id": sid}}
    s._checkpoint_deadline_ts = deadline
    # In reality the runner sets the sole-resumer flag for the whole
    # span a checkpoint is open (and beyond, through the re-stream).
    # Model "checkpoint open" as both set together.
    s._adversary_checkpoint_active = deadline is not None
    app.state.sessions[sid] = s
    return s


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class TestAdversaryAckEndpoint(unittest.TestCase):

    def _client(self):
        from consultants.server.app import create_app
        app = create_app(run_council=None, start_reaper=False)
        return TestClient(app), app

    def test_sets_ack_flag_without_reinvoking_graph(self):
        c, app = self._client()
        s = _install_running_session(app, "csl-ack", deadline=2000.0)
        r = c.post("/v1/consult/csl-ack/adversary-ack", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["acked"])
        self.assertTrue(body["checkpoint_open"])
        self.assertEqual(body["deadline_ts"], 2000.0)
        # the flag is set for the runner to read+clear
        self.assertTrue(s.take_adversary_ack())

    def test_checkpoint_open_false_when_no_deadline(self):
        c, app = self._client()
        _install_running_session(app, "csl-ack2", deadline=None)
        r = c.post("/v1/consult/csl-ack2/adversary-ack", json={})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["checkpoint_open"])

    def test_404_when_unknown(self):
        c, _app = self._client()
        r = c.post("/v1/consult/nope/adversary-ack", json={})
        self.assertEqual(r.status_code, 404)

    def test_409_when_completed(self):
        c, app = self._client()
        _install_running_session(app, "csl-done", status="completed")
        r = c.post("/v1/consult/csl-done/adversary-ack", json={})
        self.assertEqual(r.status_code, 409)

    def test_cancel_releases_the_park(self):
        # /cancel sets the ack flag so the runner's wait loop breaks
        # promptly instead of blocking the full timeout.
        c, app = self._client()
        s = _install_running_session(app, "csl-cxl", deadline=5000.0)
        r = c.post("/v1/consult/csl-cxl/cancel", json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(s.take_adversary_ack())

    def test_resume_during_checkpoint_delegates_to_ack_no_invoke(self):
        # Sole-resumer guard: when a checkpoint is open, POST /resume must
        # NOT submit an executor invoke (that double-resumes the live
        # runner). It delegates to the ack path instead. The guard returns
        # BEFORE the langgraph.types.Command import, so this needs no
        # langgraph.
        c, app = self._client()
        s = _install_running_session(app, "csl-resume", deadline=3000.0)
        s._compiled = _InvokeCounter()
        r = c.post("/v1/consult/csl-resume/resume",
                   json={"value": {"approve": True}})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "adversary_ack")
        self.assertTrue(body["acked"])
        # the graph was NOT re-invoked
        self.assertEqual(s._compiled.invoke_calls, 0)
        # the runner will read+clear the ack
        self.assertTrue(s.take_adversary_ack())

    def test_resume_during_restream_window_still_delegates(self):
        # THE BLOCKER regression guard. After the wait ends, the runner
        # clears _checkpoint_deadline_ts (→ None) but is STILL re-streaming
        # (rewind / auto-resume), so _adversary_checkpoint_active stays
        # True. A /resume in THIS window — deadline already None — must
        # still delegate to ack, never invoke. The pre-fix guard read the
        # (now-None) deadline and fell through to the executor invoke,
        # double-resuming the live runner.
        c, app = self._client()
        s = _install_running_session(app, "csl-restream", deadline=None)
        # runner owns the graph mid re-stream: active True, deadline None
        s._adversary_checkpoint_active = True
        s._compiled = _InvokeCounter()
        r = c.post("/v1/consult/csl-restream/resume", json={"value": {}})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "adversary_ack")
        self.assertFalse(body["checkpoint_open"])  # no wait open, but active
        self.assertEqual(s._compiled.invoke_calls, 0)  # NOT double-resumed


# ============================================================== #
# 5. Recorder round-trip (Last-Event-ID replay)
# ============================================================== #

class TestAwaitingAdversaryReplay(unittest.TestCase):

    def test_recorded_row_replays_via_list_runtime_events(self):
        from consultants.engine.recorder import MessageRecorder, RecorderMeta
        with TemporaryDirectory() as td:
            rec = MessageRecorder(
                Path(td) / "transcript.db",
                meta=RecorderMeta(
                    sid="csl-replay", cwd="/tmp/proj",
                    question="q", effort="high", topology="council",
                    models={"synthesizer": "m:cloud"}),
            )
            try:
                rec.record_event(
                    kind="awaiting_adversary",
                    payload={"deadline_ts": 4242.0, "timeout_s": 600.0,
                             "self_confidence": 0.5,
                             "reason": "adversary_checkpoint"},
                )
                rows = rec.list_runtime_events(since_event_id=0)
                hits = [r for r in rows if r["kind"] == "awaiting_adversary"]
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["payload"]["deadline_ts"], 4242.0)
                # Last-Event-ID resume: nothing after the row's own id
                self.assertEqual(
                    rec.list_runtime_events(since_event_id=hits[0]["event_id"]),
                    [],
                )
            finally:
                rec.close()


if __name__ == "__main__":
    unittest.main()
