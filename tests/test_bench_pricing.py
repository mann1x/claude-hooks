"""The dated price snapshot and how a recorded run is priced."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.consultants import pricing  # noqa: E402

UTC = timezone.utc


class ModelKeyTests(unittest.TestCase):

    def test_tags_map_to_rows(self):
        for tag, key in (("gemma4:31b-cloud", "gemma4"),
                         ("kimi-k2.6:cloud", "kimi-k2.6"),
                         ("gpt-oss:120b-cloud", "gpt-oss:120b"),
                         ("qwen3.5:397b-cloud", "qwen3.5:397b"),
                         ("glm-5.3-flash:cloud", "glm-5.3-flash"),
                         ("deepseek-v4.1-flash:cloud", "deepseek-v4.1-flash")):
            self.assertEqual(pricing.model_key(tag), key, tag)

    def test_a_model_off_the_page_is_unpriced_not_guessed(self):
        for tag in ("gemini-3-flash-preview:cloud", "qwen3.5:cloud",
                    "qwen3-coder-next:cloud", ""):
            self.assertIsNone(pricing.model_key(tag), tag)


class OffPeakTests(unittest.TestCase):

    def test_the_window(self):
        wed = lambda h: datetime(2026, 9, 23, h, 0, tzinfo=UTC)  # noqa: E731
        self.assertTrue(pricing.is_off_peak(wed(11)))
        self.assertFalse(pricing.is_off_peak(wed(12)))
        self.assertFalse(pricing.is_off_peak(wed(17)))
        self.assertTrue(pricing.is_off_peak(wed(18)))
        self.assertTrue(pricing.is_off_peak(datetime(2026, 9, 26, 14, tzinfo=UTC)))

    def test_off_peak_halves_deepseek_only(self):
        peak = datetime(2026, 9, 23, 14, tzinfo=UTC)
        off = datetime(2026, 9, 23, 8, tzinfo=UTC)
        self.assertEqual(pricing.rates("deepseek-v4-pro:cloud", off), (0.66, 1.98))
        self.assertEqual(pricing.rates("deepseek-v4-pro:cloud", peak), (1.32, 3.96))
        self.assertEqual(pricing.rates("glm-5.3:cloud", off),
                         pricing.rates("glm-5.3:cloud", peak))


class CostTests(unittest.TestCase):

    def test_a_million_of_each(self):
        self.assertAlmostEqual(
            pricing.cost_usd("minimax-m3:cloud", 1_000_000, 1_000_000), 3.00)

    def test_usage_cost_lists_what_it_could_not_price(self):
        usage = {"coder": {"model": "glm-5.3-flash:cloud", "prompt": 2_000_000,
                           "completion": 0},
                 "judge": {"model": "gemini-3-flash-preview:cloud",
                           "prompt": 500, "completion": 50}}
        out = pricing.usage_cost(usage)
        self.assertAlmostEqual(out["total"], 0.30)
        self.assertEqual(out["unpriced"], ["judge"])
        self.assertNotIn("judge", out["by_role"])

    def test_the_snapshot_is_dated(self):
        self.assertEqual(pricing.PRICING_DATE, "2026-09-23")
        self.assertIn("ollama.com/pricing", pricing.PRICING_SOURCE)


if __name__ == "__main__":
    unittest.main()
