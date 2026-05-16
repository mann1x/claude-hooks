"""Tests for :mod:`consultants.server.control` — the pure-Python
builders backing the HTTP control surface.

These cover the validation + shape contracts that the M9 FastAPI
routes will rely on. Nothing langgraph-specific here; the M9 route
plumbing tests live separately.
"""

from __future__ import annotations

import time
import unittest

from consultants.engine.state_v2 import Doc, InterruptState
from consultants.server.control import (
    CancelRequest,
    ControlInputError,
    InterruptResume,
    VALID_INJECT_ROLES,
    VALID_STRICTNESS_VALUES,
    VALID_TOOL_PERMISSION_VALUES,
    build_cancel_request,
    build_inject_delta,
    build_interrupt_delta,
    build_resume_command,
    build_runtime_control_delta,
    summarize_state_for_get,
)


# ============================================================== #
# build_inject_delta
# ============================================================== #

class TestBuildInjectDelta(unittest.TestCase):

    def test_happy_path_returns_doc_in_channel(self):
        delta = build_inject_delta(role="researcher", text="Check GDPR.")
        self.assertIn("additional_context", delta)
        docs = delta["additional_context"]
        self.assertEqual(len(docs), 1)
        d = docs[0]
        self.assertIsInstance(d, Doc)
        self.assertEqual(d.role, "researcher")
        self.assertEqual(d.text, "Check GDPR.")
        self.assertEqual(d.source, "user")
        # ts defaults to time.time() — within a second of now.
        self.assertAlmostEqual(d.ts, time.time(), delta=2.0)

    def test_explicit_ts_propagates(self):
        delta = build_inject_delta(
            role="any", text="hello", ts=42.5,
        )
        self.assertEqual(delta["additional_context"][0].ts, 42.5)

    def test_strips_text_whitespace(self):
        delta = build_inject_delta(
            role="researcher", text="  padded   \n",
        )
        self.assertEqual(delta["additional_context"][0].text, "padded")

    def test_rejects_unknown_role(self):
        with self.assertRaises(ControlInputError):
            build_inject_delta(role="orchestrator", text="x")

    def test_rejects_empty_text(self):
        with self.assertRaises(ControlInputError):
            build_inject_delta(role="researcher", text="")
        with self.assertRaises(ControlInputError):
            build_inject_delta(role="researcher", text="   ")

    def test_rejects_oversize_text(self):
        with self.assertRaises(ControlInputError):
            build_inject_delta(role="researcher", text="x" * 50_001)

    def test_accepts_all_valid_roles(self):
        for r in VALID_INJECT_ROLES:
            delta = build_inject_delta(role=r, text="hello")
            self.assertEqual(delta["additional_context"][0].role, r)


# ============================================================== #
# build_runtime_control_delta
# ============================================================== #

class TestBuildRuntimeControlDelta(unittest.TestCase):

    def test_deadline_ts_float(self):
        d = build_runtime_control_delta({"deadline_ts": 1234567.0})
        self.assertEqual(d["runtime_control"]["deadline_ts"], 1234567.0)

    def test_max_rounds_int(self):
        d = build_runtime_control_delta({"max_rounds": 5})
        self.assertEqual(d["runtime_control"]["max_rounds"], 5)

    def test_max_rounds_rejects_negative(self):
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({"max_rounds": -1})

    def test_max_rounds_rejects_non_int(self):
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({"max_rounds": 3.14})

    def test_confidence_target_range(self):
        d = build_runtime_control_delta({"confidence_target": 0.65})
        self.assertEqual(d["runtime_control"]["confidence_target"], 0.65)

    def test_confidence_target_rejects_out_of_range(self):
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({"confidence_target": 1.5})
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({"confidence_target": -0.1})

    def test_critic_strictness_accepts_known_values(self):
        for v in VALID_STRICTNESS_VALUES:
            d = build_runtime_control_delta({"critic_strictness": v})
            self.assertEqual(d["runtime_control"]["critic_strictness"], v)

    def test_critic_strictness_rejects_unknown(self):
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({"critic_strictness": "harsh"})

    def test_enabled_roles_must_be_list_of_strings(self):
        d = build_runtime_control_delta({
            "enabled_roles": ["planner", "researcher"],
        })
        self.assertEqual(
            d["runtime_control"]["enabled_roles"],
            ["planner", "researcher"],
        )
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({"enabled_roles": ["planner", 7]})
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({"enabled_roles": "planner"})

    def test_tool_permissions_validates_values(self):
        for perm in VALID_TOOL_PERMISSION_VALUES:
            d = build_runtime_control_delta({
                "tool_permissions": {"read_file": perm},
            })
            self.assertEqual(
                d["runtime_control"]["tool_permissions"]["read_file"], perm,
            )
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({
                "tool_permissions": {"read_file": "maybe"},
            })

    def test_boolean_flags(self):
        d = build_runtime_control_delta({
            "review_before_synthesis": True,
            "interrupt_on_low_confidence": True,
        })
        rc = d["runtime_control"]
        self.assertIs(rc["review_before_synthesis"], True)
        self.assertIs(rc["interrupt_on_low_confidence"], True)

    def test_per_lane_hard_s_positive(self):
        d = build_runtime_control_delta({"per_lane_hard_s": 1800.0})
        self.assertEqual(d["runtime_control"]["per_lane_hard_s"], 1800.0)
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({"per_lane_hard_s": 0})

    def test_unknown_key_rejected(self):
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({"frobnicate": True})

    def test_empty_body_rejected(self):
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta({})

    def test_non_dict_body_rejected(self):
        with self.assertRaises(ControlInputError):
            build_runtime_control_delta(["list", "is", "wrong"])  # type: ignore

    def test_returns_only_validated_keys(self):
        """The returned dict is the partial — caller's free to merge
        more keys later, but this builder only returns what was
        validated. (Reducer at the state level does the merge.)"""
        d = build_runtime_control_delta({
            "max_rounds": 5,
            "confidence_target": 0.7,
        })
        self.assertEqual(
            set(d["runtime_control"].keys()),
            {"max_rounds", "confidence_target"},
        )


# ============================================================== #
# build_interrupt_delta
# ============================================================== #

class TestBuildInterruptDelta(unittest.TestCase):

    def test_default_reason(self):
        d = build_interrupt_delta()
        rc = d["runtime_control"]
        self.assertIs(rc["pause_requested"], True)
        self.assertEqual(rc["pause_reason"], "user-pause")

    def test_explicit_reason(self):
        d = build_interrupt_delta(reason="want to inject context")
        self.assertEqual(
            d["runtime_control"]["pause_reason"],
            "want to inject context",
        )

    def test_empty_reason_falls_back_to_default(self):
        d = build_interrupt_delta(reason="   ")
        self.assertEqual(
            d["runtime_control"]["pause_reason"], "user-pause"
        )


# ============================================================== #
# build_resume_command
# ============================================================== #

class TestBuildResumeCommand(unittest.TestCase):

    def test_returns_interrupt_resume(self):
        r = build_resume_command({"approve": True}, decision="approve")
        self.assertIsInstance(r, InterruptResume)
        self.assertEqual(r.value, {"approve": True})
        self.assertEqual(r.decision, "approve")

    def test_decision_optional(self):
        r = build_resume_command(None)
        self.assertEqual(r.decision, "")

    def test_passes_through_arbitrary_value(self):
        r = build_resume_command("just a string")
        self.assertEqual(r.value, "just a string")


# ============================================================== #
# build_cancel_request
# ============================================================== #

class TestBuildCancelRequest(unittest.TestCase):

    def test_default(self):
        c = build_cancel_request()
        self.assertIsInstance(c, CancelRequest)
        self.assertFalse(c.discard_partial)
        rc = c.state_delta["runtime_control"]
        self.assertIs(rc["cancel_requested"], True)
        self.assertEqual(rc["cancel_reason"], "user-cancel")

    def test_discard_partial_flag(self):
        c = build_cancel_request(discard_partial=True)
        self.assertTrue(c.discard_partial)

    def test_explicit_reason(self):
        c = build_cancel_request(reason="user gave up")
        self.assertEqual(
            c.state_delta["runtime_control"]["cancel_reason"],
            "user gave up",
        )


# ============================================================== #
# summarize_state_for_get
# ============================================================== #

class TestSummarizeStateForGet(unittest.TestCase):

    def test_basic_summary_shape(self):
        state = {
            "runtime_control": {"max_rounds": 3},
            "plan_items": ["a", "b", "c"],
            "research": ["r1", "r2"],
            "confidence": [0.4, 0.7],
            "partial_synthesis": "draft",
            "research_rounds_used": 2,
            "critic_reroutes_used": 1,
            "critic_decision": "ready",
            "additional_context": [
                Doc(role="researcher", text="extra ctx", ts=100.0),
            ],
            "final_answer": "",
        }
        out = summarize_state_for_get(state, sid="csl-1")
        self.assertEqual(out["sid"], "csl-1")
        self.assertEqual(out["runtime_control"], {"max_rounds": 3})
        self.assertEqual(out["research_count"], 2)
        self.assertEqual(out["plan_items_count"], 3)
        self.assertEqual(out["research_rounds_used"], 2)
        self.assertEqual(out["critic_reroutes_used"], 1)
        self.assertEqual(out["critic_decision"], "ready")
        self.assertEqual(out["latest_confidence"], 0.7)
        self.assertEqual(out["partial_synthesis"], "draft")
        self.assertFalse(out["final_answer_ready"])
        # additional_context serializes the dataclass fields.
        self.assertEqual(len(out["additional_context"]), 1)
        self.assertEqual(out["additional_context"][0]["role"], "researcher")
        self.assertEqual(out["additional_context"][0]["text"], "extra ctx")

    def test_interrupt_state_dataclass_round_trip(self):
        state = {
            "interrupt_state": InterruptState(
                kind="review", prompt="Approve?",
                posted_at=42.0, payload={"draft": "body"},
            ),
        }
        out = summarize_state_for_get(state, sid="x")
        it = out["interrupt_state"]
        self.assertEqual(it["kind"], "review")
        self.assertEqual(it["prompt"], "Approve?")
        self.assertEqual(it["posted_at"], 42.0)
        self.assertEqual(it["payload"], {"draft": "body"})

    def test_interrupt_state_none(self):
        out = summarize_state_for_get({}, sid="x")
        self.assertIsNone(out["interrupt_state"])

    def test_final_answer_ready_truthy(self):
        out = summarize_state_for_get(
            {"final_answer": "Done."}, sid="x",
        )
        self.assertTrue(out["final_answer_ready"])

    def test_handles_langgraph_snapshot_shape(self):
        # When the caller passes the result of compiled.get_state(...),
        # it has a {"values": {...}, "next": (...,), "tasks": [...]}
        # shape rather than the flat state dict.
        snapshot = {
            "values": {"research": ["r1"], "final_answer": ""},
            "next": ("synthesizer",),
            "tasks": [
                {"name": "synthesizer",
                 "interrupts": [
                     {"value": {"kind": "review", "prompt": "Approve?"}},
                 ]},
            ],
        }
        out = summarize_state_for_get(snapshot, sid="x")
        self.assertEqual(out["research_count"], 1)
        self.assertEqual(out["next"], ("synthesizer",))
        self.assertEqual(len(out["tasks"]), 1)
        self.assertEqual(out["tasks"][0]["name"], "synthesizer")
        self.assertEqual(
            out["tasks"][0]["interrupts"][0]["kind"], "review",
        )


if __name__ == "__main__":
    unittest.main()
