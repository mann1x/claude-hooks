"""Tests for ``consultants.engine.control`` — RuntimeControl
defaults + accessor helpers + the v2-integrated ``route_after_critic``
in ``consultants.engine.council``.

Pure-Python tests; run on the main ``claude-hooks`` env without
langgraph (control + council both import cleanly there).
"""

from __future__ import annotations

import time
import unittest

from consultants import config as cc
from consultants.engine import control
from consultants.engine.council import route_after_critic


class TestTimeTargetFor(unittest.TestCase):

    def test_low_no_fanout(self):
        soft, hard = control.time_target_for("low", 0)
        self.assertEqual(soft, 60.0)
        self.assertEqual(hard, 180.0)   # × 3.0

    def test_medium_no_fanout(self):
        soft, hard = control.time_target_for("medium", 0)
        self.assertEqual(soft, 180.0)
        self.assertEqual(hard, 540.0)

    def test_xhigh_with_two_extras(self):
        # 600 + 2 × 180 = 960 soft, × 3 = 2880 hard.
        soft, hard = control.time_target_for("xhigh", 2)
        self.assertEqual(soft, 960.0)
        self.assertEqual(hard, 2880.0)

    def test_xauto_uses_wider_hard_multiplier(self):
        soft, hard = control.time_target_for("xauto", 0)
        self.assertEqual(soft, 720.0)
        self.assertEqual(hard, 720.0 * 4.0)   # 4× not 3×

    def test_unknown_effort_falls_back_to_medium(self):
        soft, hard = control.time_target_for("bogus", 0)
        self.assertEqual(soft, 180.0)
        self.assertEqual(hard, 540.0)

    def test_negative_extras_clamped_to_zero(self):
        soft, hard = control.time_target_for("xhigh", -5)
        # 600 + 0 × 180 = 600.
        self.assertEqual(soft, 600.0)


class TestRuntimeControlDefaults(unittest.TestCase):

    def _cfg(self) -> cc.ConsultantsConfig:
        return cc.ConsultantsConfig()

    def test_medium_baseline(self):
        rc = control.runtime_control_defaults(
            self._cfg(), effort="medium", now_ts=1000.0,
        )
        self.assertEqual(rc["soft_target_ts"], 1180.0)   # +180
        self.assertEqual(rc["deadline_ts"], 1540.0)      # +540
        self.assertEqual(rc["per_lane_hard_s"], 3600.0)
        self.assertEqual(rc["max_rounds"], 1)
        self.assertEqual(rc["max_reroutes"], 1)
        self.assertEqual(rc["confidence_target"], 0.7)
        self.assertEqual(rc["critic_strictness"], "normal")
        self.assertEqual(rc["stall_threshold_s"], 300.0)
        self.assertEqual(rc["stall_retries"], 1)
        self.assertEqual(rc["tool_permissions"], {})
        self.assertEqual(rc["xauto_escalations"], 0)

    def test_xauto_starts_at_xmedium(self):
        rc = control.runtime_control_defaults(
            self._cfg(), effort="xauto", now_ts=1000.0,
        )
        self.assertEqual(rc["xauto_tier"], "xmedium")
        # xauto uses the wider 4× multiplier:  720 × 4 = 2880.
        self.assertEqual(rc["deadline_ts"], 1000.0 + 2880.0)
        # Starts modest — escalator advances.
        self.assertEqual(rc["max_rounds"], 1)
        self.assertEqual(rc["max_reroutes"], 1)

    def test_xhigh_with_extras_extends_deadline(self):
        rc = control.runtime_control_defaults(
            self._cfg(), effort="xhigh",
            n_fanout_extras=2, now_ts=0.0,
        )
        # 600 + 2 × 180 = 960 soft; × 3 = 2880 hard.
        self.assertEqual(rc["soft_target_ts"], 960.0)
        self.assertEqual(rc["deadline_ts"], 2880.0)
        self.assertEqual(rc["xauto_tier"], "xhigh")
        self.assertEqual(rc["max_rounds"], 3)
        self.assertEqual(rc["max_reroutes"], 2)

    def test_high_tier_caps(self):
        rc = control.runtime_control_defaults(
            self._cfg(), effort="high", now_ts=0.0,
        )
        self.assertEqual(rc["max_rounds"], 3)
        self.assertEqual(rc["max_reroutes"], 2)

    def test_enabled_roles_reflects_config(self):
        cfg = self._cfg()
        # Default config has all 4 roles enabled. Disable critic and
        # check the live list matches.
        cfg.roles["critic"].enabled = False
        rc = control.runtime_control_defaults(cfg, effort="medium")
        self.assertNotIn("critic", rc["enabled_roles"])
        self.assertIn("synthesizer", rc["enabled_roles"])


class TestAccessorHelpers(unittest.TestCase):

    def test_runtime_get_default_when_absent(self):
        self.assertEqual(
            control.runtime_get({}, "max_rounds", default=42),
            42,
        )

    def test_runtime_get_returns_value_when_present(self):
        state = {"runtime_control": {"max_rounds": 7}}
        self.assertEqual(
            control.runtime_get(state, "max_rounds", default=42),
            7,
        )

    def test_runtime_get_default_when_value_is_none(self):
        state = {"runtime_control": {"max_rounds": None}}
        self.assertEqual(
            control.runtime_get(state, "max_rounds", default=42),
            42,
        )

    def test_runtime_max_rounds_with_fallback(self):
        self.assertEqual(
            control.runtime_max_rounds({}, fallback=3),
            3,
        )
        self.assertEqual(
            control.runtime_max_rounds(
                {"runtime_control": {"max_rounds": 10}}, fallback=3,
            ),
            10,
        )

    def test_runtime_deadline_passed_no_deadline(self):
        self.assertFalse(control.runtime_deadline_passed({}))
        self.assertFalse(control.runtime_deadline_passed(
            {"runtime_control": {}}
        ))

    def test_runtime_deadline_passed_future(self):
        state = {"runtime_control":
                 {"deadline_ts": time.time() + 60.0}}
        self.assertFalse(control.runtime_deadline_passed(state))

    def test_runtime_deadline_passed_past(self):
        state = {"runtime_control":
                 {"deadline_ts": time.time() - 60.0}}
        self.assertTrue(control.runtime_deadline_passed(state))

    def test_runtime_deadline_now_ts_override(self):
        state = {"runtime_control": {"deadline_ts": 1000.0}}
        self.assertTrue(control.runtime_deadline_passed(
            state, now_ts=1001.0))
        self.assertFalse(control.runtime_deadline_passed(
            state, now_ts=999.0))

    def test_runtime_enabled_roles_fallback(self):
        self.assertEqual(
            control.runtime_enabled_roles(
                {}, fallback=("planner", "researcher", "synthesizer"),
            ),
            ("planner", "researcher", "synthesizer"),
        )

    def test_runtime_enabled_roles_live_override(self):
        state = {"runtime_control":
                 {"enabled_roles": ["researcher", "synthesizer"]}}
        self.assertEqual(
            control.runtime_enabled_roles(
                state, fallback=("planner", "researcher",
                                 "critic", "synthesizer"),
            ),
            ("researcher", "synthesizer"),
        )

    def test_runtime_critic_strictness_default_normal(self):
        self.assertEqual(control.runtime_critic_strictness({}), "normal")

    def test_runtime_critic_strictness_live(self):
        state = {"runtime_control": {"critic_strictness": "strict"}}
        self.assertEqual(
            control.runtime_critic_strictness(state),
            "strict",
        )


class TestRouteAfterCriticV2(unittest.TestCase):
    """The v1 ``route_after_critic`` now reads RuntimeControl with
    v1-caps fallback. Pin both behaviors here."""

    def _base_state(self, **kw) -> dict:
        s = {
            "critic_decision": "needs_more_research",
            "research_rounds_used": 1,
            "critic_reroutes_used": 0,
            "effort": "high",   # high: max_rounds=3, max_reroutes=2
        }
        s.update(kw)
        return s

    def test_ready_decision_short_circuits_to_synthesizer(self):
        state = self._base_state(critic_decision="ready")
        self.assertEqual(route_after_critic(state), "synthesizer")

    def test_v1_fallback_when_no_runtime_control(self):
        # high tier, 1 round used + 0 reroutes — under both caps.
        state = self._base_state()
        self.assertEqual(route_after_critic(state), "researcher")

    def test_v1_fallback_hits_cap(self):
        # high tier, 3 rounds used (== cap) — must short-circuit.
        state = self._base_state(research_rounds_used=3)
        self.assertEqual(route_after_critic(state), "synthesizer")

    def test_runtime_control_overrides_max_rounds_lower(self):
        # high tier's v1 cap is 3 rounds. With runtime mutation to 1,
        # the same state (1 round used) hits the cap and exits.
        state = self._base_state(
            runtime_control={"max_rounds": 1},
        )
        self.assertEqual(route_after_critic(state), "synthesizer")

    def test_runtime_control_overrides_max_rounds_higher(self):
        # max tier's v1 cap is 8. Run high with a runtime override of
        # 10 — at 3 rounds we keep going (v1 would have stopped at 3).
        state = self._base_state(
            effort="high",
            research_rounds_used=3,
            runtime_control={"max_rounds": 10},
        )
        self.assertEqual(route_after_critic(state), "researcher")

    def test_runtime_deadline_passed_short_circuits(self):
        # Even when caps allow another round, a passed deadline
        # forces synthesis.
        state = self._base_state(
            runtime_control={
                "max_rounds": 10,
                "deadline_ts": time.time() - 60.0,
            },
        )
        self.assertEqual(route_after_critic(state), "synthesizer")

    def test_runtime_control_overrides_max_reroutes(self):
        # 2 reroutes used; v1 cap = 2 → would exit. Runtime bumps to 5.
        state = self._base_state(
            critic_reroutes_used=2,
            runtime_control={"max_reroutes": 5},
        )
        self.assertEqual(route_after_critic(state), "researcher")

    def test_runtime_max_reroutes_lower_than_v1(self):
        # high tier v1 = 2 reroutes; runtime tightens to 0.
        state = self._base_state(
            critic_reroutes_used=0,
            runtime_control={"max_reroutes": 0},
        )
        self.assertEqual(route_after_critic(state), "synthesizer")


if __name__ == "__main__":
    unittest.main()
