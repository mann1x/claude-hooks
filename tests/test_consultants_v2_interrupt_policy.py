"""Tests for :mod:`consultants.engine.interrupt_policy`.

Pure-Python decision logic; no LangGraph required. Covers:

- ``should_interrupt_before_synthesis`` static-review fires only when
  configured AND not already mid-interrupt.
- ``should_interrupt_on_low_confidence`` opt-in flag + threshold
  semantics (RuntimeControl override, explicit arg override).
- ``should_interrupt_on_tool_permission`` distinguishes
  allow/deny/ask.
- ``should_interrupt_on_user_pause`` honors the cooperative flag and
  re-entry guard.
- ``InterruptDecision.to_payload`` / ``to_interrupt_state`` shape
  contracts.
- ``clear_interrupt`` returns the correct state delta.
"""

from __future__ import annotations

import types
import unittest

from consultants.engine.interrupt_policy import (
    InterruptDecision,
    clear_interrupt,
    should_interrupt_before_synthesis,
    should_interrupt_on_low_confidence,
    should_interrupt_on_tool_permission,
    should_interrupt_on_user_pause,
)
from consultants.engine.state_v2 import InterruptState


# ============================================================== #
# InterruptDecision shape
# ============================================================== #

class TestInterruptDecisionShape(unittest.TestCase):

    def test_to_payload_has_required_keys(self):
        d = InterruptDecision(
            kind="review", prompt="Review draft",
            payload={"foo": "bar"},
        )
        payload = d.to_payload()
        self.assertEqual(payload["kind"], "review")
        self.assertEqual(payload["prompt"], "Review draft")
        self.assertEqual(payload["payload"], {"foo": "bar"})
        self.assertFalse(payload["urgent"])

    def test_to_payload_copies_payload_dict(self):
        original = {"x": 1}
        d = InterruptDecision(kind="review", prompt="P", payload=original)
        out = d.to_payload()
        out["payload"]["x"] = 99
        # Original Decision instance untouched (we returned a copy).
        self.assertEqual(d.payload["x"], 1)

    def test_to_interrupt_state_round_trips_fields(self):
        d = InterruptDecision(
            kind="tool_permission", prompt="Approve read?",
            payload={"tool": "read_file"}, urgent=True,
        )
        s = d.to_interrupt_state(posted_at=1234.0)
        self.assertIsInstance(s, InterruptState)
        self.assertEqual(s.kind, "tool_permission")
        self.assertEqual(s.prompt, "Approve read?")
        self.assertEqual(s.posted_at, 1234.0)
        self.assertEqual(s.payload, {"tool": "read_file"})


# ============================================================== #
# Static review-before-synthesis
# ============================================================== #

class TestReviewBeforeSynthesis(unittest.TestCase):

    def test_fires_when_runtime_control_flag_set(self):
        state = {
            "runtime_control": {"review_before_synthesis": True},
            "research": ["r1", "r2"],
            "plan": "A plan",
        }
        d = should_interrupt_before_synthesis(state)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "review")
        self.assertIn("research report", d.prompt)
        self.assertEqual(d.payload["research_count"], 2)

    def test_fires_when_cfg_runtime_flag_set(self):
        cfg = types.SimpleNamespace(
            runtime=types.SimpleNamespace(review_before_synthesis=True),
        )
        state = {"research": []}
        d = should_interrupt_before_synthesis(state, cfg=cfg)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "review")

    def test_does_not_fire_when_both_flags_off(self):
        state = {"runtime_control": {}, "research": ["r1"]}
        self.assertIsNone(should_interrupt_before_synthesis(state))

    def test_skips_when_interrupt_already_active(self):
        # post-resume guard: state.interrupt_state lingers until the
        # node that consumed the resume clears it. While it's set,
        # never re-fire.
        state = {
            "runtime_control": {"review_before_synthesis": True},
            "interrupt_state": InterruptState(
                kind="review", prompt="x", posted_at=1.0,
            ),
        }
        self.assertIsNone(should_interrupt_before_synthesis(state))

    def test_payload_includes_partial_synthesis(self):
        state = {
            "runtime_control": {"review_before_synthesis": True},
            "partial_synthesis": "Draft body",
            "research": [],
        }
        d = should_interrupt_before_synthesis(state)
        self.assertEqual(d.payload["partial_synthesis"], "Draft body")

    def test_payload_truncates_plan_excerpt(self):
        state = {
            "runtime_control": {"review_before_synthesis": True},
            "plan": "x" * 1000,
        }
        d = should_interrupt_before_synthesis(state)
        self.assertEqual(len(d.payload["plan_excerpt"]), 500)


# ============================================================== #
# Dynamic low-confidence
# ============================================================== #

class TestLowConfidence(unittest.TestCase):

    def test_does_not_fire_when_flag_off(self):
        # Default policy is OFF — confidence < threshold normally
        # drives xauto escalation, not a human interrupt.
        state = {
            "runtime_control": {},
            "confidence": [0.2],
        }
        self.assertIsNone(should_interrupt_on_low_confidence(state))

    def test_does_not_fire_when_no_confidence_emitted(self):
        # Pre-condition: at least one score must have been emitted.
        state = {
            "runtime_control": {"interrupt_on_low_confidence": True},
            "confidence": [],
        }
        self.assertIsNone(should_interrupt_on_low_confidence(state))

    def test_fires_below_explicit_threshold(self):
        state = {
            "runtime_control": {"interrupt_on_low_confidence": True},
            "confidence": [0.4],
        }
        d = should_interrupt_on_low_confidence(state, threshold=0.7)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "low_confidence")
        self.assertEqual(d.payload["score"], 0.4)
        self.assertEqual(d.payload["threshold"], 0.7)

    def test_uses_runtime_control_target_when_threshold_none(self):
        state = {
            "runtime_control": {
                "interrupt_on_low_confidence": True,
                "confidence_target": 0.8,
            },
            "confidence": [0.5],
        }
        d = should_interrupt_on_low_confidence(state)
        self.assertIsNotNone(d)
        self.assertEqual(d.payload["threshold"], 0.8)

    def test_does_not_fire_at_or_above_threshold(self):
        state = {
            "runtime_control": {"interrupt_on_low_confidence": True},
            "confidence": [0.7],
        }
        self.assertIsNone(
            should_interrupt_on_low_confidence(state, threshold=0.7)
        )

    def test_uses_latest_score(self):
        # latest_confidence reads [-1]; a later high score MUST
        # suppress the interrupt even though earlier scores were low.
        state = {
            "runtime_control": {"interrupt_on_low_confidence": True},
            "confidence": [0.2, 0.95],
        }
        self.assertIsNone(
            should_interrupt_on_low_confidence(state, threshold=0.7)
        )


# ============================================================== #
# Tool-permission
# ============================================================== #

class TestToolPermission(unittest.TestCase):

    def test_fires_on_ask(self):
        state = {
            "runtime_control": {
                "tool_permissions": {"write_file": "ask"},
            },
        }
        d = should_interrupt_on_tool_permission(
            state, "write_file", args_preview='path="x.txt"',
        )
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "tool_permission")
        self.assertEqual(d.payload["tool"], "write_file")
        self.assertEqual(d.payload["args_preview"], 'path="x.txt"')
        # Urgent — block the call site until the human decides.
        self.assertTrue(d.urgent)

    def test_no_fire_on_allow_or_missing(self):
        state = {
            "runtime_control": {
                "tool_permissions": {"read_file": "allow"},
            },
        }
        self.assertIsNone(
            should_interrupt_on_tool_permission(state, "read_file")
        )
        self.assertIsNone(
            should_interrupt_on_tool_permission(state, "glob")
        )

    def test_no_fire_on_deny(self):
        # deny is handled by the caller (skip the tool call entirely);
        # this function only returns an interrupt for "ask".
        state = {
            "runtime_control": {
                "tool_permissions": {"survey_project": "deny"},
            },
        }
        self.assertIsNone(
            should_interrupt_on_tool_permission(state, "survey_project")
        )

    def test_args_preview_truncated_to_200(self):
        state = {
            "runtime_control": {
                "tool_permissions": {"x": "ask"},
            },
        }
        d = should_interrupt_on_tool_permission(
            state, "x", args_preview="A" * 500,
        )
        self.assertIsNotNone(d)
        self.assertEqual(len(d.payload["args_preview"]), 200)


# ============================================================== #
# User-pause
# ============================================================== #

class TestUserPause(unittest.TestCase):

    def test_fires_when_pause_requested(self):
        state = {
            "runtime_control": {"pause_requested": True},
            "research": ["r1"],
        }
        d = should_interrupt_on_user_pause(state, role="researcher")
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "user_pause")
        self.assertEqual(d.payload["role"], "researcher")
        self.assertEqual(d.payload["research_count"], 1)

    def test_no_fire_when_flag_off(self):
        state = {"runtime_control": {}}
        self.assertIsNone(should_interrupt_on_user_pause(state))

    def test_no_fire_when_interrupt_active(self):
        state = {
            "runtime_control": {"pause_requested": True},
            "interrupt_state": InterruptState(
                kind="review", prompt="x", posted_at=1.0,
            ),
        }
        self.assertIsNone(should_interrupt_on_user_pause(state))


# ============================================================== #
# clear_interrupt
# ============================================================== #

class TestClearInterrupt(unittest.TestCase):

    def test_returns_state_delta_clearing_both_fields(self):
        d = clear_interrupt({})
        self.assertIsNone(d["interrupt_state"])
        self.assertEqual(
            d["runtime_control"], {"pause_requested": False}
        )


if __name__ == "__main__":
    unittest.main()
