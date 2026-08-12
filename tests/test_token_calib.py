"""Token calibration — claude_hooks/token_calib.py.

Ported from the Cline fork's ``tokens.ts``. Each test names the live
failure its constant or branch exists to prevent, because the numbers
are indefensible without them.
"""

import unittest

from claude_hooks import token_calib as tc


class TestDefaults(unittest.TestCase):
    def test_two_ratios_not_one(self):
        """Reasoning is denser prose than serialized JSON, and the mix
        moves every turn, so averaging them is wrong in the direction
        that lets a request be built too large."""
        self.assertGreater(tc.CHARS_PER_TOKEN, tc.THINKING_CHARS_PER_TOKEN)

    def test_prompt_default_overcounts(self):
        """3 rather than the conventional 4, so a trigger fires before
        the provider rejects the request."""
        self.assertLessEqual(tc.CHARS_PER_TOKEN, 3.0)

    def test_zero_chars_is_zero_tokens(self):
        self.assertEqual(tc.estimate_tokens(0), 0)
        self.assertEqual(tc.estimate_thinking_tokens(0), 0)


class TestCalibration(unittest.TestCase):
    def test_first_observation_is_taken_whole(self):
        """The default it replaces is a guess, not a measurement."""
        tc.observe_request_tokens(6000, 1000, model="m")
        self.assertAlmostEqual(tc.chars_per_token("m"), 6.0, places=3)

    def test_later_observations_are_smoothed(self):
        tc.observe_request_tokens(6000, 1000, model="m")
        tc.observe_request_tokens(2000, 1000, model="m")
        ratio = tc.chars_per_token("m")
        self.assertGreater(ratio, 2.0)
        self.assertLess(ratio, 6.0)

    def test_gemma4_ratio_is_not_discarded(self):
        """645,803 chars for 78,138 tokens = 8.26. A ceiling of 8
        discarded that and every later observation, freezing the ratio
        while the estimate it fed ran 1.7x high."""
        tc.observe_request_tokens(645_803, 78_138, model="gemma4")
        self.assertAlmostEqual(tc.chars_per_token("gemma4"), 8.26, places=1)

    def test_impossible_ratios_are_rejected(self):
        for chars, tokens in ((10, 1000), (1_000_000, 1)):
            tc.reset_calibration("m")
            tc.observe_request_tokens(chars, tokens, model="m")
            self.assertEqual(tc.chars_per_token("m"), tc.CHARS_PER_TOKEN)

    def test_a_rejected_ratio_does_not_take_the_count_with_it(self):
        """Only one of the two can be wrong. ``tokens`` is what the
        provider counted; nothing about the character measurement can
        make that untrue. Keeping them together froze the fork's
        last-observed count fourteen turns stale."""
        tc.observe_request_tokens(10, 1000, model="m")     # ratio rejected
        self.assertEqual(tc.last_observed_request_tokens("m"), 1000)
        self.assertEqual(tc.chars_per_token("m"), tc.CHARS_PER_TOKEN)

    def test_reasoning_is_charged_at_its_own_rate_first(self):
        """Otherwise both halves of the estimate account for the same
        characters and the prompt ratio learns about prose."""
        tc.observe_request_tokens(10_000, 2_000, model="plain")
        tc.observe_request_tokens(10_000, 2_000, 5_000, model="mixed")
        self.assertNotAlmostEqual(tc.chars_per_token("plain"),
                                  tc.chars_per_token("mixed"), places=2)

    def test_thinking_ratio_calibrates_separately(self):
        tc.observe_thinking_tokens(2_700, 1_000, model="m")
        self.assertAlmostEqual(tc.thinking_chars_per_token("m"), 2.7,
                               places=2)
        self.assertEqual(tc.chars_per_token("m"), tc.CHARS_PER_TOKEN)


class TestPerModelKeying(unittest.TestCase):
    """The council runs N models concurrently in one process (x-tier
    fanout). A blended ratio would describe neither."""

    def test_models_do_not_share_a_ratio(self):
        tc.observe_request_tokens(8_000, 1_000, model="big-vocab")
        tc.observe_request_tokens(2_000, 1_000, model="small-vocab")
        self.assertAlmostEqual(tc.chars_per_token("big-vocab"), 8.0, places=2)
        self.assertAlmostEqual(tc.chars_per_token("small-vocab"), 2.0,
                               places=2)

    def test_unnamed_model_is_its_own_bucket(self):
        tc.observe_request_tokens(8_000, 1_000, model="named")
        self.assertEqual(tc.chars_per_token(), tc.CHARS_PER_TOKEN)

    def test_reset_can_target_one_model(self):
        tc.observe_request_tokens(8_000, 1_000, model="a")
        tc.observe_request_tokens(2_000, 1_000, model="b")
        tc.reset_calibration("a")
        self.assertEqual(tc.chars_per_token("a"), tc.CHARS_PER_TOKEN)
        self.assertAlmostEqual(tc.chars_per_token("b"), 2.0, places=2)


class TestOutputCapAttribution(unittest.TestCase):
    def test_window_sources_are_window_bound(self):
        for source in tc.WINDOW_BOUND_SOURCES:
            self.assertTrue(tc.OutputCapReport(source=source).window_bound)

    def test_request_and_model_caps_are_not(self):
        """Shrinking the transcript cannot raise them, so compacting
        spends the transcript to change nothing."""
        for source in ("requested", "model-max-output", "default",
                       "uncapped"):
            self.assertFalse(tc.OutputCapReport(source=source).window_bound)

    def test_cap_is_read_not_consumed(self):
        """A standing fact about the last request, overwritten by the
        next — a reader asking 'was that cap the window's?' wants the
        answer to survive being asked."""
        tc.note_output_cap(tc.OutputCapReport(source="requested"), "m")
        self.assertIsNotNone(tc.last_output_cap("m"))
        self.assertIsNotNone(tc.last_output_cap("m"))

    def test_overflow_is_consumed_not_read(self):
        """Consumed so a single overflow forces a single compaction;
        left set it would force one on every following turn, including
        the ones it had already made room for."""
        tc.note_context_overflow(tc.ContextOverflowReport(
            context_window=1000, estimated_input_tokens=999,
            remaining_context=1, min_output_tokens=100), "m")
        self.assertIsNotNone(tc.consume_context_overflow("m"))
        self.assertIsNone(tc.consume_context_overflow("m"))


class TestRequestMeasurement(unittest.TestCase):
    def test_reasoning_chars_are_counted_under_every_spelling(self):
        """The runtime says ``reasoning``; Ollama and the transcript say
        ``thinking``. A filter that knew one would silently do nothing
        on the other."""
        for key in ("thinking", "reasoning", "reasoning_content"):
            _, reasoning = tc.measure_request_chars(
                [{"role": "assistant", "content": "x", key: "y" * 500}])
            self.assertGreaterEqual(reasoning, 500, key)

    def test_no_reasoning_uses_the_plain_ratio(self):
        msgs = [{"role": "user", "content": "a" * 900}]
        chars, reasoning = tc.measure_request_chars(msgs)
        self.assertEqual(reasoning, 0)
        self.assertEqual(tc.estimate_request_tokens(msgs),
                         tc.estimate_tokens(chars))

    def test_reasoning_heavy_request_is_not_undercounted(self):
        """The direction that lets a request be built too large."""
        plain = [{"role": "user", "content": "a" * 3000}]
        thinky = [{"role": "assistant", "content": "a" * 100,
                   "thinking": "b" * 2900}]
        self.assertGreater(tc.estimate_request_tokens(thinky),
                           tc.estimate_request_tokens(plain) * 0.9)

    def test_unserializable_content_does_not_raise(self):
        class Odd:
            def __repr__(self):
                return "odd"
        chars, _ = tc.measure_request_chars(
            [{"role": "user", "content": Odd()}])
        self.assertGreater(chars, 0)

    def test_reasoning_never_exceeds_total(self):
        chars, reasoning = tc.measure_request_chars(
            [{"role": "assistant", "thinking": "x" * 5000}])
        self.assertLessEqual(reasoning, chars)


if __name__ == "__main__":
    unittest.main()
