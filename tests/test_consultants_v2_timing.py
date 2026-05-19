"""Tests for ``consultants.engine.timing`` — pure prompt-injection
helpers that render the soft time budget for planner / researcher
prompts.

Pure-Python, no langgraph. Lives in the main ``claude-hooks`` test
env.
"""

from __future__ import annotations

import unittest

from consultants.engine import timing


class TestFormatMinutes(unittest.TestCase):
    """Spot-check the duration formatter — sub-minute rounds to 5s,
    sub-hour rounds to whole min, ≥ hour splits h + min."""

    def test_seconds_under_minute(self):
        self.assertEqual(timing._format_minutes(45.0), "45 s")

    def test_seconds_rounds_to_5(self):
        # 47s rounds to 45s; 48s rounds to 50s.
        self.assertEqual(timing._format_minutes(47.0), "45 s")
        self.assertEqual(timing._format_minutes(48.0), "50 s")

    def test_sub_second_clamps_to_minimum(self):
        self.assertEqual(timing._format_minutes(0.5), "5 s")

    def test_one_minute_exact(self):
        self.assertEqual(timing._format_minutes(60.0), "1 min")

    def test_three_minutes(self):
        self.assertEqual(timing._format_minutes(180.0), "3 min")

    def test_rounds_to_nearest_minute(self):
        self.assertEqual(timing._format_minutes(189.0), "3 min")
        self.assertEqual(timing._format_minutes(210.0), "4 min")  # 3.5 → 4

    def test_one_hour_exact(self):
        self.assertEqual(timing._format_minutes(3600.0), "1 h")

    def test_hour_with_minutes(self):
        self.assertEqual(timing._format_minutes(3900.0), "1 h 5 min")

    def test_hours_round_to_minute(self):
        # 1h 5.4min rounds to 1h 5min; 1h 5.6min rounds to 1h 6min.
        self.assertEqual(timing._format_minutes(3624.0), "1 h")
        self.assertEqual(timing._format_minutes(3960.0), "1 h 6 min")


class TestPlannerSoftTargetBlock(unittest.TestCase):

    def test_empty_state_returns_empty_string(self):
        self.assertEqual(timing.planner_soft_target_block({}), "")

    def test_runtime_control_without_targets_returns_empty(self):
        # Missing both soft_target_ts and deadline_ts → no block.
        state = {"runtime_control": {"max_rounds": 3}}
        self.assertEqual(timing.planner_soft_target_block(state), "")

    def test_renders_full_block_with_soft_and_hard(self):
        state = {"runtime_control": {
            "soft_target_ts": 1000.0 + 180.0,  # 3 min from now
            "deadline_ts":    1000.0 + 540.0,  # 9 min from now
        }}
        out = timing.planner_soft_target_block(state, now_ts=1000.0)
        self.assertIn("SOFT TIME TARGET", out)
        self.assertIn("3 min", out)
        self.assertIn("hard cap", out)
        self.assertIn("9 min", out)
        self.assertIn("Plan the number and depth", out)

    def test_zero_remaining_clamps_to_zero(self):
        # Now is past the soft target — should render "0 s" not negative.
        state = {"runtime_control": {
            "soft_target_ts": 1000.0,
            "deadline_ts":    2000.0,
        }}
        out = timing.planner_soft_target_block(state, now_ts=1500.0)
        # The soft remaining is 0 (clamped); hard remaining is 500s.
        # Should not contain a negative number.
        self.assertNotIn("-", out)
        self.assertIn("0 s", out)  # or similar — at least not negative

    def test_soft_only_no_hard(self):
        state = {"runtime_control": {"soft_target_ts": 1180.0}}
        out = timing.planner_soft_target_block(state, now_ts=1000.0)
        self.assertIn("SOFT TIME TARGET", out)
        self.assertIn("3 min", out)
        self.assertNotIn("hard cap", out)

    def test_hard_only_no_soft(self):
        state = {"runtime_control": {"deadline_ts": 1540.0}}
        out = timing.planner_soft_target_block(state, now_ts=1000.0)
        self.assertNotIn("SOFT TIME TARGET", out)
        self.assertIn("hard cap", out)
        self.assertIn("9 min", out)


class TestResearcherRemainingBlock(unittest.TestCase):

    def test_empty_state(self):
        self.assertEqual(timing.researcher_remaining_block({}), "")

    def test_renders_remaining_budget(self):
        state = {"runtime_control": {
            "soft_target_ts": 1180.0,
            "deadline_ts":    1540.0,
        }}
        out = timing.researcher_remaining_block(state, now_ts=1060.0)
        self.assertIn("REMAINING TIME BUDGET", out)
        # 1540 - 1060 = 480s = 8 min remaining until hard cap.
        self.assertIn("8 min", out)
        # 1180 - 1060 = 120s = 2 min soft.
        self.assertIn("2 min", out)

    def test_soft_target_passed_shows_waiting(self):
        state = {"runtime_control": {
            "soft_target_ts": 1180.0,
            "deadline_ts":    1540.0,
        }}
        # now > soft_target_ts but < deadline_ts.
        out = timing.researcher_remaining_block(state, now_ts=1300.0)
        self.assertIn("REMAINING TIME BUDGET", out)
        self.assertIn("Soft target has passed", out)
        # Hard cap remaining: 1540 - 1300 = 240s = 4 min.
        self.assertIn("4 min", out)

    def test_no_soft_target_only_hard(self):
        state = {"runtime_control": {"deadline_ts": 2000.0}}
        out = timing.researcher_remaining_block(state, now_ts=1000.0)
        self.assertIn("REMAINING TIME BUDGET", out)
        # 17 min remaining (1000s, rounds to 17 min).
        self.assertIn("17 min", out)
        self.assertNotIn("Soft target", out)


class TestTimePressureSignal(unittest.TestCase):

    def test_no_runtime_control_returns_none(self):
        self.assertEqual(timing.time_pressure_signal({}), "none")

    def test_no_soft_target_returns_none(self):
        state = {"runtime_control": {"deadline_ts": 2000.0}}
        self.assertEqual(timing.time_pressure_signal(state), "none")

    def test_ample_when_more_than_half_remains(self):
        # Soft 1000s from now, hard 3000s from now → original soft ~ 1000s,
        # ratio = 1.0, ample.
        state = {"runtime_control": {
            "soft_target_ts": 1000.0 + 800.0,
            "deadline_ts":    1000.0 + 3000.0,
        }}
        self.assertEqual(
            timing.time_pressure_signal(state, now_ts=1000.0),
            "ample",
        )

    def test_critical_past_soft_target(self):
        # now is past the soft target → ratio negative → critical.
        state = {"runtime_control": {
            "soft_target_ts": 1000.0,
            "deadline_ts":    1000.0 + 300.0,
        }}
        self.assertEqual(
            timing.time_pressure_signal(state, now_ts=1100.0),
            "critical",
        )

    def test_critical_returns_string_form(self):
        # Pure sanity — the return is a known string for unique-value
        # dispatch tables in consumers.
        state = {"runtime_control": {
            "soft_target_ts": 1000.0,
            "deadline_ts":    1500.0,
        }}
        signal = timing.time_pressure_signal(state, now_ts=999.5)
        self.assertIn(signal, {"ample", "normal", "tight", "critical"})


if __name__ == "__main__":
    unittest.main()
