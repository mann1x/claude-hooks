"""Tests for ``consultants.engine.events`` — typed event taxonomy +
emit() bridge — and ``MessageRecorder.record_event`` /
``list_runtime_events`` extensions.

Two surfaces:

1. **Event dataclasses** — frozen, JSON-serializable, hashable;
   the per-class ``kind`` discriminator gets the right default.
2. **emit()** — defensive no-op outside a runnable context (so
   nodes that emit during tests don't blow up), delegates to
   LangGraph's ``get_stream_writer`` when in one.
3. **Recorder integration** — ``record_event`` round-trips event
   dicts through ``runtime_events``, and ``list_runtime_events``
   pages results with ``Last-Event-ID`` semantics.

Pure-Python; LangGraph patched out for the emit() tests so this
file runs cleanly in the main ``claude-hooks`` env.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from consultants.engine import events as ev
from consultants.engine.events import (
    ConfidenceUpdate,
    CouncilEvent,
    DeadlineWarning,
    Interrupt,
    NodeFinished,
    NodeStarted,
    PartialSynthesis,
    Resumed,
    RuntimeMutation,
    ToolCall,
    emit,
)
from consultants.engine.recorder import MessageRecorder, RecorderMeta


# ============================================================== #
# 1. Dataclass shape + discriminator + serialization
# ============================================================== #

class TestEventDataclassShape(unittest.TestCase):

    def test_node_started_kind_default(self):
        e = NodeStarted(role="researcher", lane_idx=2, model="glm")
        self.assertEqual(e.kind, "node_started")
        self.assertEqual(e.role, "researcher")
        self.assertEqual(e.lane_idx, 2)
        self.assertEqual(e.model, "glm")
        self.assertEqual(e.round, 1)   # default
        self.assertGreater(e.ts, 0.0)

    def test_node_finished_carries_duration_and_error(self):
        e = NodeFinished(role="critic", round=2, duration_ms=1234,
                          ok=False, error="upstream 500")
        self.assertEqual(e.kind, "node_finished")
        self.assertEqual(e.duration_ms, 1234)
        self.assertFalse(e.ok)
        self.assertEqual(e.error, "upstream 500")

    def test_tool_call_truncations(self):
        e = ToolCall(role="researcher", tool="read_file",
                      args_preview="path=/x/y.py:1-100",
                      output_preview="def foo():...",
                      duration_ms=15)
        self.assertEqual(e.kind, "tool_call")
        self.assertEqual(e.tool, "read_file")

    def test_partial_synthesis(self):
        e = PartialSynthesis(text="Hello", draft_index=1)
        self.assertEqual(e.kind, "partial_synthesis")
        self.assertEqual(e.text, "Hello")

    def test_confidence_update(self):
        e = ConfidenceUpdate(score=0.62, source="synthesizer",
                              target=0.70)
        self.assertEqual(e.kind, "confidence_update")
        self.assertEqual(e.score, 0.62)
        self.assertEqual(e.target, 0.70)

    def test_deadline_warning(self):
        e = DeadlineWarning(remaining_s=120.0,
                             soft_target_passed=True,
                             hard_cap_passed=False)
        self.assertEqual(e.kind, "deadline_warning")
        self.assertTrue(e.soft_target_passed)

    def test_runtime_mutation(self):
        e = RuntimeMutation(changes={"max_rounds": 5},
                             reason="user control")
        self.assertEqual(e.kind, "runtime_mutation")
        self.assertEqual(e.changes, {"max_rounds": 5})

    def test_interrupt(self):
        e = Interrupt(interrupt_kind="review_before_synthesis",
                       payload_preview="Draft is ready",
                       thread_id="csl-x")
        self.assertEqual(e.kind, "interrupt")
        self.assertEqual(e.interrupt_kind, "review_before_synthesis")

    def test_resumed(self):
        e = Resumed(interrupt_kind="review_before_synthesis",
                     decision="approve")
        self.assertEqual(e.kind, "resumed")
        self.assertEqual(e.decision, "approve")

    def test_events_are_frozen(self):
        """Sanity: dataclasses are immutable. Catches accidental
        mutability creep on later refactors."""
        e = NodeStarted(role="planner")
        with self.assertRaises(FrozenInstanceError):
            e.role = "researcher"   # type: ignore[misc]

    def test_to_dict_is_json_serializable(self):
        e = NodeStarted(role="researcher", lane_idx=1,
                         model="kimi-k2.6:cloud", sid="csl-x")
        d = e.to_dict()
        # Round-trip through JSON.
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["role"], "researcher")
        self.assertEqual(decoded["kind"], "node_started")
        self.assertEqual(decoded["sid"], "csl-x")
        self.assertIn("ts", decoded)

    def test_explicit_ts_override(self):
        e = NodeStarted(role="r", ts=1234.5)
        self.assertEqual(e.ts, 1234.5)

    def test_explicit_kind_override_allowed(self):
        """``kind`` has a per-class default but is overridable; we
        rely on that for v2/v3 backward-compat (a new subclass
        could choose a more specific kind string)."""
        e = NodeStarted(role="r", kind="researcher_started_v2")
        self.assertEqual(e.kind, "researcher_started_v2")


# ============================================================== #
# 2. emit() — defensive bridge to get_stream_writer
# ============================================================== #

class TestEmitDefensive(unittest.TestCase):

    def test_emit_outside_runnable_context_returns_false(self):
        """We're not inside a compiled graph here. ``emit()`` must
        catch the RuntimeError and silently return False — that's
        the property that lets node functions be tested as plain
        Python without raising."""
        # Real call against real langgraph (if installed); else the
        # ImportError path returns False too.
        e = NodeStarted(role="planner")
        result = emit(e)
        self.assertFalse(result)

    def test_emit_returns_true_when_writer_succeeds(self):
        """Patch get_stream_writer to return a recording callable;
        emit should call it with the dict form."""
        captured: list[dict] = []
        fake_writer = captured.append

        # The function imports get_stream_writer inside the function
        # body, so we patch ``langgraph.config.get_stream_writer``
        # at the source.
        fake_config = type(
            "fake_module", (), {"get_stream_writer": lambda: fake_writer},
        )
        import sys
        sys.modules["langgraph.config"] = fake_config
        try:
            e = NodeFinished(role="planner", duration_ms=42, ok=True)
            ok = emit(e)
            self.assertTrue(ok)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["kind"], "node_finished")
            self.assertEqual(captured[0]["duration_ms"], 42)
        finally:
            # Restore — don't leak the fake into other test modules.
            del sys.modules["langgraph.config"]

    def test_emit_writer_exception_returns_false_no_raise(self):
        def boom_writer(_payload):
            raise RuntimeError("downstream consumer crashed")

        fake_config = type(
            "fake_module", (), {"get_stream_writer": lambda: boom_writer},
        )
        import sys
        sys.modules["langgraph.config"] = fake_config
        try:
            result = emit(NodeStarted(role="r"))
            self.assertFalse(result)
        finally:
            del sys.modules["langgraph.config"]


# ============================================================== #
# 3. Recorder integration — record_event + list_runtime_events
# ============================================================== #

def _new_recorder(d: Path) -> MessageRecorder:
    return MessageRecorder(
        d / "test.db",
        meta=RecorderMeta(
            sid="csl-test", cwd=str(d),
            question="q", effort="medium", topology="council",
            models={"researcher": "m"},
        ),
    )


class TestRecorderRecordEvent(unittest.TestCase):

    def test_record_event_persists_payload(self):
        with tempfile.TemporaryDirectory() as d:
            rec = _new_recorder(Path(d))
            try:
                rec.record_event(
                    kind="node_started",
                    role="researcher", round=1, lane_idx=2,
                    payload={"kind": "node_started",
                              "role": "researcher",
                              "model": "kimi-k2.6:cloud",
                              "ts": 1700.0},
                )
                events = rec.list_runtime_events()
                self.assertEqual(len(events), 1)
                row = events[0]
                self.assertEqual(row["kind"], "node_started")
                self.assertEqual(row["role"], "researcher")
                self.assertEqual(row["round"], 1)
                self.assertEqual(row["lane_idx"], 2)
                self.assertEqual(row["ts"], 1700.0)
                self.assertEqual(row["payload"]["model"], "kimi-k2.6:cloud")
            finally:
                rec.finalize(status="completed")

    def test_record_event_kind_required(self):
        with tempfile.TemporaryDirectory() as d:
            rec = _new_recorder(Path(d))
            try:
                with self.assertRaises(ValueError):
                    rec.record_event(kind="", payload={})
            finally:
                rec.finalize(status="completed")

    def test_record_event_payload_optional(self):
        with tempfile.TemporaryDirectory() as d:
            rec = _new_recorder(Path(d))
            try:
                rec.record_event(kind="heartbeat")
                events = rec.list_runtime_events()
                self.assertEqual(len(events), 1)
                # Payload defaults to {"kind": "heartbeat"} (the
                # method backfills kind into the payload for
                # consumer convenience).
                self.assertEqual(events[0]["payload"]["kind"],
                                  "heartbeat")
            finally:
                rec.finalize(status="completed")

    def test_record_event_no_op_when_closed(self):
        with tempfile.TemporaryDirectory() as d:
            rec = _new_recorder(Path(d))
            # ``finalize()`` flips status but doesn't close; ``close()``
            # is what flips the ``_closed`` flag the no-op guard
            # checks. Mirror the rest of the recorder's API: a
            # recorder you've finalized is still writable until
            # explicitly closed.
            rec.finalize(status="completed")
            rec.close()
            # Closed recorder must not raise on record_event.
            rec.record_event(kind="node_started", payload={})
            # And the list call should return an empty list (closed
            # path short-circuits before opening a connection).
            self.assertEqual(rec.list_runtime_events(), [])

    def test_list_runtime_events_paging(self):
        with tempfile.TemporaryDirectory() as d:
            rec = _new_recorder(Path(d))
            try:
                for i in range(5):
                    rec.record_event(
                        kind="t", role="r", round=1, lane_idx=i,
                        payload={"i": i},
                    )
                # Read all.
                all_events = rec.list_runtime_events()
                self.assertEqual(len(all_events), 5)
                # Resume from event_id 2 — should get rows 3, 4, 5.
                tail = rec.list_runtime_events(since_event_id=2)
                self.assertEqual(len(tail), 3)
                self.assertEqual(tail[0]["event_id"], 3)
                self.assertEqual(tail[0]["payload"]["i"], 2)
                # Limit.
                first = rec.list_runtime_events(limit=2)
                self.assertEqual(len(first), 2)
                self.assertEqual(first[0]["event_id"], 1)
                self.assertEqual(first[1]["event_id"], 2)
            finally:
                rec.finalize(status="completed")

    def test_list_runtime_events_payload_parse_error_safe(self):
        """A corrupt payload row should NOT crash the lister —
        return a sentinel ``__parse_error__`` in the payload dict."""
        with tempfile.TemporaryDirectory() as d:
            rec = _new_recorder(Path(d))
            try:
                # Bypass the normal API to inject malformed JSON.
                conn = rec._conn()
                conn.execute(
                    """
                    INSERT INTO runtime_events (
                        ts, kind, role, round, lane_idx, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (time.time(), "bad", "r", 1, 0, "not-json"),
                )
                conn.commit()
                rows = rec.list_runtime_events()
                self.assertEqual(len(rows), 1)
                self.assertIn("__parse_error__", rows[0]["payload"])
            finally:
                rec.finalize(status="completed")

    def test_record_event_round_trip_from_dataclass(self):
        """Persist an event dataclass via to_dict() through
        record_event; read it back via list_runtime_events; the
        consumer can reconstruct the original."""
        with tempfile.TemporaryDirectory() as d:
            rec = _new_recorder(Path(d))
            try:
                e = NodeStarted(role="researcher", lane_idx=1,
                                 model="kimi", round=2, ts=1700.0)
                rec.record_event(
                    kind=e.kind,
                    role=e.role, round=e.round, lane_idx=e.lane_idx,
                    payload=e.to_dict(),
                )
                rows = rec.list_runtime_events()
                self.assertEqual(len(rows), 1)
                payload = rows[0]["payload"]
                self.assertEqual(payload["kind"], "node_started")
                self.assertEqual(payload["role"], "researcher")
                self.assertEqual(payload["lane_idx"], 1)
                self.assertEqual(payload["model"], "kimi")
                self.assertEqual(payload["round"], 2)
            finally:
                rec.finalize(status="completed")


# ============================================================== #
# 4. Stall layer integration — record_event surfaces stall events
# ============================================================== #

class TestStallIntegration(unittest.TestCase):
    """End-to-end: a chat_with_stall_protection call with on_event
    pointing at recorder.record_event lands rows in runtime_events."""

    def test_stall_event_lands_in_runtime_events(self):
        from consultants.engine.stall_chat import (
            make_stall_protected_chat_fn,
        )

        def chat_streamed(payload, *, on_token=None, cancel_check=None):
            if on_token:
                on_token("t")
            return {"choices": [{"message": {"content": "ok"}}],
                    "usage": {}}

        with tempfile.TemporaryDirectory() as d:
            rec = _new_recorder(Path(d))
            try:
                def sink(ev_dict):
                    rec.record_event(
                        kind=ev_dict.get("kind") or "stall",
                        role="researcher", round=1,
                        payload=ev_dict,
                    )
                chat_fn = make_stall_protected_chat_fn(
                    chat_streamed,
                    stall_threshold_s=1.0, hard_cap_s=5.0,
                    retries=0, check_interval_s=0.05,
                    retry_backoff_s=0.01,
                    on_event=sink,
                )
                chat_fn({"model": "stub"})
                rows = rec.list_runtime_events()
                kinds = [r["kind"] for r in rows]
                self.assertIn("stall.attempt.ok", kinds)
            finally:
                rec.finalize(status="completed")


if __name__ == "__main__":
    unittest.main()
