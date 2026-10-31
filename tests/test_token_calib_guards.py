"""Calibration guards, the anchored estimate and per-session keys.

Ported from the Cline fork's ``sdk/packages/shared/src/llms/tokens.test.ts``
(plan ``docs/PLAN-council-cerebriline.md`` A4). Numbers and the incidents
behind them are the fork's; where our keying deliberately differs, the
test says so.
"""

import unittest
import unittest.mock

from claude_hooks import token_calib as tc
from claude_hooks.agent_loop import runner


class TestWindowGuard(unittest.TestCase):
    def test_drops_a_count_larger_than_the_context_window(self):
        """Harness run 20260918-010944-0364: 166,848 tokens reported
        against a 131,072 window, at a ratio (2.65) the pairing guards
        accept. Kept as the anchor it overestimated the next request
        2.06x and tripped the overflow check at 43% of the window."""
        tc.observe_request_tokens(353_282, 100_182, model="m")
        calibrated = tc.chars_per_token("m")
        tc.observe_request_tokens(441_771, 166_848, model="m",
                                  context_window=131_072)
        self.assertEqual(tc.chars_per_token("m"), calibrated)
        self.assertEqual(tc.last_observed_request_tokens("m"), 100_182)

    def test_keeps_a_count_that_fills_the_window_exactly(self):
        """A full window is the ordinary state just before compaction."""
        tc.observe_request_tokens(393_216, 131_072, model="m",
                                  context_window=131_072)
        self.assertEqual(tc.last_observed_request_tokens("m"), 131_072)
        self.assertAlmostEqual(tc.chars_per_token("m"), 3.0, places=2)

    def test_keeps_a_count_when_no_window_is_known(self):
        tc.observe_request_tokens(441_771, 166_848, model="m")
        self.assertEqual(tc.last_observed_request_tokens("m"), 166_848)


class TestFloorGuard(unittest.TestCase):
    def test_drops_a_count_that_claims_a_token_per_character(self):
        """pandorum session 1789201117876_5t3as: 138,549 tokens for
        138,262 characters. Kept, it compacted a 45,783-token transcript
        and reported it as 138.5k (fork c61cb3600). The old code stored
        the count before any check."""
        tc.observe_request_tokens(353_282, 100_182, model="m")
        calibrated = tc.chars_per_token("m")
        tc.observe_request_tokens(138_262, 138_549, model="m")
        self.assertEqual(tc.chars_per_token("m"), calibrated)
        self.assertEqual(tc.last_observed_request_tokens("m"), 100_182)

    def test_above_the_ceiling_the_count_stands(self):
        tc.observe_request_tokens(3_000, 1_000, model="m")
        tc.observe_request_tokens(1_000, 1, model="m")
        self.assertEqual(tc.last_observed_request_tokens("m"), 1)
        self.assertAlmostEqual(tc.chars_per_token("m"), 3.0, places=3)

    def test_unusable_reports_are_ignored(self):
        tc.observe_request_tokens(3_000, 1_000, model="m")
        for chars, tokens in ((3_000, 0), (3_000, -5), (0, 1_000)):
            tc.observe_request_tokens(chars, tokens, model="m")
        self.assertEqual(tc.last_observed_request_tokens("m"), 1_000)

    def test_follows_the_context_down_after_it_shrinks(self):
        tc.observe_request_tokens(353_282, 100_182, model="m")
        tc.observe_request_tokens(80_000, 22_661, model="m")
        self.assertEqual(tc.last_observed_request_tokens("m"), 22_661)


class TestResidualGuard(unittest.TestCase):
    def test_refuses_a_ratio_whose_reasoning_term_ate_the_count(self):
        """300,000 reasoning chars at 2.7 claim 111,111 of 120,000
        tokens, leaving 8,889 to explain 100,000 characters: 11.25
        chars/token that says nothing about the content."""
        tc.observe_request_tokens(400_000, 120_000, 300_000, model="m",
                                  session="s1")
        self.assertEqual(tc.chars_per_token("m"), tc.CHARS_PER_TOKEN)
        self.assertEqual(tc.chars_per_token("m", "s1"), tc.CHARS_PER_TOKEN)

    def test_keeps_the_count_when_it_refuses_the_ratio(self):
        tc.observe_request_tokens(400_000, 120_000, 300_000, model="m",
                                  session="s1")
        self.assertEqual(tc.last_observed_request_tokens("m", "s1"),
                         120_000)

    def test_takes_an_observation_whose_residual_carries_signal(self):
        tc.observe_request_tokens(400_000, 120_000, 40_000, model="m",
                                  session="s1")
        self.assertNotEqual(tc.chars_per_token("m", "s1"),
                            tc.CHARS_PER_TOKEN)


class TestAnchoredEstimate(unittest.TestCase):
    def test_growth_that_is_all_reasoning_is_charged_at_that_rate(self):
        tc.observe_request_tokens(400_000, 100_000, 200_000, model="m",
                                  session="s1")
        self.assertEqual(
            tc.anchored_request_tokens(450_000, 250_000, "m", "s1"), 118_519)

    def test_unchanged_arithmetic_without_reasoning(self):
        tc.observe_request_tokens(360_000, 100_000, 0, model="m",
                                  session="s1")
        self.assertEqual(tc.anchored_request_tokens(396_000, 0, "m", "s1"),
                         110_000)

    def test_falls_back_when_reasoning_would_consume_the_anchor(self):
        tc.observe_request_tokens(400_000, 10_000, 300_000, model="m",
                                  session="s1")
        self.assertEqual(
            tc.anchored_request_tokens(440_000, 300_000, "m", "s1"), 11_000)

    def test_plain_estimate_before_anything_is_measured(self):
        self.assertEqual(tc.anchored_request_tokens(30_000, 0, "m", "s1"),
                         tc.estimate_tokens(30_000, "m"))

    def test_counts_only_the_characters_added_since(self):
        tc.observe_request_tokens(400_000, 70_000, 0, model="m",
                                  session="s1")
        anchored = tc.anchored_request_tokens(420_000, 0, "m", "s1")
        self.assertGreater(anchored, 70_000)
        self.assertLess(anchored, 76_000)

    def test_the_anchor_holds_the_measured_count(self):
        tc.observe_request_tokens(400_000, 70_000, 0, model="m",
                                  session="s1")
        self.assertEqual(tc.anchored_request_tokens(400_000, 0, "m", "s1"),
                         70_000)

    def test_shrinks_after_a_compaction(self):
        tc.observe_request_tokens(400_000, 70_000, 0, model="m",
                                  session="s1")
        anchored = tc.anchored_request_tokens(150_000, 0, "m", "s1")
        self.assertLess(anchored, 70_000)
        self.assertGreater(anchored, 0)

    def test_ignores_an_anchor_from_another_conversation(self):
        tc.observe_request_tokens(400_000, 70_000, 0, model="m",
                                  session="s1")
        # s2 reads the model-level prior (5.71), not s1's anchor.
        self.assertEqual(tc.anchored_request_tokens(420_000, 0, "m", "s2"),
                         tc.estimate_tokens(420_000, "m"))

    def test_estimate_request_tokens_uses_the_session_anchor(self):
        msgs = [{"role": "user", "content": "x" * 10_000}]
        chars, _ = tc.measure_request_chars(msgs)
        tc.observe_request_tokens(chars, 1_234, model="m", session="s1")
        self.assertEqual(
            tc.estimate_request_tokens(msgs, model="m", session="s1"), 1_234)


class TestSessionKeys(unittest.TestCase):
    """Ours differs from the fork on purpose: its owner check lets an
    unnamed reader see any session's count. Here a session's anchor is
    never on the model-level state a sibling lane reads (plan A4)."""

    def test_a_session_answers_only_with_its_own_count(self):
        tc.observe_request_tokens(360_000, 100_000, model="m",
                                  session="lane-a")
        self.assertEqual(tc.last_observed_request_tokens("m", "lane-a"),
                         100_000)
        self.assertIsNone(tc.last_observed_request_tokens("m", "lane-b"))
        self.assertIsNone(tc.last_observed_request_tokens("m"))

    def test_the_model_ratio_is_the_cold_start_prior(self):
        tc.observe_request_tokens(80_000, 10_000, model="m",
                                  session="lane-a")
        self.assertAlmostEqual(tc.chars_per_token("m", "lane-b"), 8.0,
                               places=2)

    def test_a_session_ratio_is_its_own_after_it_measures(self):
        tc.observe_request_tokens(80_000, 10_000, model="m",
                                  session="lane-a")
        tc.observe_request_tokens(20_000, 10_000, model="m",
                                  session="lane-b")
        self.assertAlmostEqual(tc.chars_per_token("m", "lane-a"), 8.0,
                               places=2)
        self.assertAlmostEqual(tc.chars_per_token("m", "lane-b"), 2.0,
                               places=2)

    def test_release_forgets_the_session(self):
        tc.observe_request_tokens(80_000, 10_000, model="m",
                                  session="lane-a")
        tc.release_session("lane-a")
        self.assertIsNone(tc.last_observed_request_tokens("m", "lane-a"))

    def test_sessions_are_bounded(self):
        with unittest.mock.patch.object(tc, "MAX_SESSIONS", 3):
            for i in range(5):
                tc.observe_request_tokens(8_000, 1_000, model="m",
                                          session=f"s{i}")
            self.assertIsNone(tc.last_observed_request_tokens("m", "s0"))
            self.assertEqual(tc.last_observed_request_tokens("m", "s4"),
                             1_000)


class TestToolLoopCalibration(unittest.TestCase):
    """Every tool-loop iteration calibrates, not only ``_single_shot``."""

    def test_on_response_fires_per_call_with_the_sent_payload(self):
        seen = []
        responses = [
            {"choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "echo", "arguments": "{}"}}]},
                "finish_reason": "tool_calls"}]},
            {"choices": [{"message": {"role": "assistant",
                                      "content": "done"},
                          "finish_reason": "stop"}]},
        ]
        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]},
            cwd="/tmp",
            config=runner.LoopConfig(
                force_first_tool_call=False,
                on_response=lambda p, r: seen.append(len(p["messages"]))),
            tool_specs=[{"type": "function", "function": {
                "name": "echo", "parameters": {"type": "object"}}}],
            chat_fn=lambda p: responses.pop(0),
            tool_executor=lambda n, a, c: "out",
        )
        self.assertEqual(seen, [1, 3])

    def test_budgeter_observe_calibrates_its_own_session(self):
        from consultants.engine import council

        class _Client:
            def context_length(self, model):
                return 262_144

        prepare = council._loop_payload_budgeter(_Client(), "m",
                                                 role="researcher")
        tools = [{"type": "function", "function": {
            "name": "read_file", "description": "d" * 2_000,
            "parameters": {"type": "object"}}}]
        payload = {"messages": [{"role": "user", "content": "q" * 6_000}],
                   "tools": tools}
        prepare(payload)
        prepare.observe(payload, {
            "choices": [{"message": {"role": "assistant", "content": "a"}}],
            "usage": {"prompt_tokens": 2_000, "completion_tokens": 10}})
        self.assertEqual(
            tc.last_observed_request_tokens("m", prepare.session), 2_000)
        # The model-level state carries the ratio, never the anchor.
        self.assertIsNone(tc.last_observed_request_tokens("m"))
        chars, _ = tc.measure_request_chars(payload["messages"],
                                            tools=tools)
        self.assertAlmostEqual(tc.chars_per_token("m"), chars / 2_000,
                               places=3)

    def test_tool_schemas_are_in_the_estimate(self):
        from consultants.engine import budget
        msgs = [{"role": "user", "content": "q"}]
        tools = [{"type": "function", "function": {
            "name": "t", "description": "d" * 9_000}}]
        self.assertGreater(
            budget.estimate_messages_tokens(msgs, "m", tools=tools),
            budget.estimate_messages_tokens(msgs, "m") + 2_500)
        self.assertGreater(budget.estimate_tools_tokens(tools, "m"), 2_500)


class TestMostlyReasoning(unittest.TestCase):
    def test_threshold_is_the_forks(self):
        self.assertEqual(tc.MOSTLY_REASONING_SHARE, 0.8)

    def test_a_turn_seventy_percent_reasoning_does_not_teach_it(self):
        from consultants.engine import council
        resp = {"choices": [{"message": {
            "role": "assistant", "content": "c" * 300,
            "thinking": "t" * 700}}]}
        council._observe_usage([{"role": "user", "content": "q"}], resp,
                               0, 300, "m")
        self.assertEqual(tc.thinking_chars_per_token("m"),
                         tc.THINKING_CHARS_PER_TOKEN)


if __name__ == "__main__":
    unittest.main()
