"""Output budget + context-window planning — consultants/engine/budget.py."""

import unittest

from consultants.engine import budget


class _Client:
    """Chat client with a context_length probe, like the Ollama one."""

    def __init__(self, value=262144, raises=False):
        self.value = value
        self.raises = raises
        self.calls = 0

    def context_length(self, model):
        self.calls += 1
        if self.raises:
            raise RuntimeError("probe exploded")
        return self.value


class _NoProbeClient:
    """Llamafile / OpenAI-compat passthrough: no probe at all."""


class TestContextLengthProbe(unittest.TestCase):
    def test_reads_the_clients_probe(self):
        self.assertEqual(
            budget.context_length_of(_Client(262144), "m"), 262144)

    def test_client_without_probe_is_unknown(self):
        self.assertIsNone(
            budget.context_length_of(_NoProbeClient(), "m"))

    def test_probe_failure_is_unknown_not_a_crash(self):
        self.assertIsNone(
            budget.context_length_of(_Client(raises=True), "m"))

    def test_nonsense_values_are_unknown(self):
        for v in (0, -1, None, "big"):
            self.assertIsNone(budget.context_length_of(_Client(v), "m"))


class TestPlan(unittest.TestCase):
    def test_requests_an_explicit_budget_on_a_roomy_window(self):
        """The incident's shape: a small prompt against a 262k window.
        Something must be sent, or the provider default rules."""
        msgs = [{"role": "user", "content": "short question"}]
        p = budget.plan(_Client(262144), "m", msgs)
        self.assertEqual(p.output_tokens, budget.TARGET_OUTPUT_TOKENS)
        self.assertFalse(p.needs_compaction)
        self.assertEqual(p.context_length, 262144)

    def test_unknown_window_still_requests_the_target(self):
        """Not knowing the window does not change why we send the
        budget: to override a default we cannot see."""
        p = budget.plan(_NoProbeClient(), "m",
                        [{"role": "user", "content": "hi"}])
        self.assertIsNone(p.context_length)
        self.assertEqual(p.output_tokens, budget.TARGET_OUTPUT_TOKENS)
        self.assertFalse(p.needs_compaction)

    def test_budget_shrinks_to_fit_a_small_window(self):
        big = "x" * (30000 * int(budget.CHARS_PER_TOKEN))
        p = budget.plan(_Client(40000), "m",
                        [{"role": "user", "content": big}])
        self.assertLess(p.output_tokens, budget.TARGET_OUTPUT_TOKENS)
        self.assertGreaterEqual(p.output_tokens, budget.MIN_OUTPUT_TOKENS)

    def test_crowded_window_demands_compaction(self):
        big = "x" * (39000 * int(budget.CHARS_PER_TOKEN))
        p = budget.plan(_Client(40000), "m",
                        [{"role": "user", "content": big}])
        self.assertTrue(p.needs_compaction)
        self.assertEqual(p.output_tokens, budget.MIN_OUTPUT_TOKENS)

    def test_token_estimate_is_conservative(self):
        """Biased to over-count, so the error leaves headroom rather
        than discovering the limit by hitting it."""
        text = "word " * 1000          # ~1000 tokens by a real tokenizer
        self.assertGreater(budget.estimate_tokens(text), 1000)


class TestEstimateMessages(unittest.TestCase):
    def test_counts_all_roles(self):
        msgs = [{"role": "system", "content": "a" * 300},
                {"role": "user", "content": "b" * 300}]
        self.assertGreater(budget.estimate_messages_tokens(msgs), 190)

    def test_tolerates_block_content_and_junk(self):
        msgs = [{"role": "user", "content": [{"text": "hello"}]},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"function": {"name": "read_file"}}]},
                "not a dict"]
        self.assertGreater(budget.estimate_messages_tokens(msgs), 0)

    def test_empty_is_zero(self):
        self.assertEqual(budget.estimate_messages_tokens([]), 0)


class TestCompaction(unittest.TestCase):
    def _history(self, n=20, size=4000):
        msgs = [{"role": "system", "content": "you are a researcher"},
                {"role": "user", "content": "the original question"}]
        for i in range(n):
            msgs.append({"role": "assistant", "content": f"note {i} " * size})
        return msgs

    def test_noop_when_it_already_fits(self):
        msgs = [{"role": "user", "content": "hi"}]
        out, changed = budget.compact_messages(msgs, keep_tokens=10000)
        self.assertFalse(changed)
        self.assertIs(out, msgs)

    def test_drops_the_middle_and_fits(self):
        msgs = self._history()
        out, changed = budget.compact_messages(msgs, keep_tokens=20000)
        self.assertTrue(changed)
        self.assertLess(len(out), len(msgs))

    def test_keeps_instructions_and_question(self):
        msgs = self._history()
        out, _ = budget.compact_messages(msgs, keep_tokens=20000)
        self.assertEqual(out[0]["content"], "you are a researcher")
        self.assertEqual(out[1]["content"], "the original question")

    def test_keeps_the_most_recent_exchanges(self):
        msgs = self._history()
        out, _ = budget.compact_messages(msgs, keep_tokens=20000,
                                         keep_recent=4)
        self.assertEqual(out[-4:], msgs[-4:])

    def test_elision_is_announced_not_silent(self):
        """A model that knows its history was abridged reasons about the
        gap differently from one that believes it has the whole record."""
        msgs = self._history()
        out, _ = budget.compact_messages(msgs, keep_tokens=20000)
        marker = [m for m in out if "elided" in (m.get("content") or "")]
        self.assertEqual(len(marker), 1)
        self.assertIn("message(s) elided", marker[0]["content"])

    def test_keeps_every_leading_system_message(self):
        msgs = ([{"role": "system", "content": "rule one"},
                 {"role": "system", "content": "rule two"}]
                + self._history()[1:])
        out, _ = budget.compact_messages(msgs, keep_tokens=20000)
        self.assertEqual(out[0]["content"], "rule one")
        self.assertEqual(out[1]["content"], "rule two")

    def test_nothing_to_drop_returns_unchanged(self):
        """Head plus tail already covers the whole list — there is no
        middle to elide, and inventing one would delete live state."""
        msgs = [{"role": "system", "content": "s" * 90000},
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"}]
        out, changed = budget.compact_messages(msgs, keep_tokens=100,
                                               keep_recent=4)
        self.assertFalse(changed)
        self.assertEqual(out, msgs)

    def test_empty_history_is_safe(self):
        out, changed = budget.compact_messages([], keep_tokens=100)
        self.assertFalse(changed)
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
