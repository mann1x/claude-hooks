"""Tests for the ## Now block injection."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks import now_block as nb  # noqa: E402


class FormatNowBlockTests(unittest.TestCase):

    def _utc_dt(self):
        return datetime(2026, 5, 2, 14, 34, 20, tzinfo=timezone.utc)

    def test_disabled_returns_empty(self):
        cfg = {"system": {"now_block": {"enabled": False}}}
        self.assertEqual(nb.format_now_block(cfg, now=self._utc_dt()), "")

    def test_enabled_emits_markdown_block(self):
        cfg = {"system": {"now_block": {"enabled": True, "timezone": "Europe/Berlin"}}}
        out = nb.format_now_block(cfg, now=self._utc_dt())
        self.assertIn("## Now", out)
        # Date and weekday shown.
        self.assertIn("2026-05-02", out)
        self.assertIn("Saturday", out)
        # Anchor reminder is the whole point of this block.
        self.assertIn("Anchor any time-dependent reasoning", out)

    def test_local_offset_format(self):
        cfg = {"system": {"now_block": {"enabled": True, "timezone": "Europe/Berlin"}}}
        out = nb.format_now_block(cfg, now=self._utc_dt())
        # Berlin in May is UTC+02:00 (CEST).
        self.assertIn("UTC+02:00", out)
        self.assertIn("Europe/Berlin", out)

    def test_naive_datetime_gets_local_tz(self):
        cfg = {"system": {"now_block": {"enabled": True}}}
        # Naive datetime — function must localise rather than crash.
        out = nb.format_now_block(cfg, now=datetime(2026, 5, 2, 12, 0, 0))
        self.assertIn("## Now", out)

    def test_invalid_zone_falls_back_silently(self):
        cfg = {"system": {"now_block": {"enabled": True, "timezone": "Not/A/Real/Zone"}}}
        out = nb.format_now_block(cfg, now=self._utc_dt())
        self.assertIn("## Now", out)


class PrependToContextTests(unittest.TestCase):

    def test_prepend_to_existing_context(self):
        cfg = {"system": {"now_block": {"enabled": True}}}
        out = nb.prepend_to_context("## Recalled memory\n\n- foo", cfg)
        self.assertIsNotNone(out)
        self.assertTrue(out.startswith("## Now"))
        self.assertIn("Recalled memory", out)
        # Separated by blank line.
        self.assertIn("\n\n## Recalled memory", out)

    def test_prepend_with_no_existing_context_returns_block(self):
        cfg = {"system": {"now_block": {"enabled": True}}}
        out = nb.prepend_to_context("", cfg)
        self.assertIsNotNone(out)
        self.assertTrue(out.startswith("## Now"))

    def test_prepend_disabled_no_recall_returns_none(self):
        cfg = {"system": {"now_block": {"enabled": False}}}
        self.assertIsNone(nb.prepend_to_context("", cfg))
        self.assertIsNone(nb.prepend_to_context(None, cfg))

    def test_prepend_disabled_with_recall_passthrough(self):
        cfg = {"system": {"now_block": {"enabled": False}}}
        out = nb.prepend_to_context("## Recalled memory\n\n- foo", cfg)
        self.assertEqual(out, "## Recalled memory\n\n- foo")


class HookIntegrationTests(unittest.TestCase):
    """The user_prompt_submit handler must surface the now-block even
    when recall returns nothing (otherwise the model never gets a
    fresh local-TZ timestamp and the whole feature is wasted)."""

    def test_handler_emits_now_block_when_recall_empty(self):
        from claude_hooks.hooks.user_prompt_submit import handle
        cfg = {
            "hooks": {"user_prompt_submit": {"enabled": True, "min_prompt_chars": 0}},
            "system": {"now_block": {"enabled": True}},
        }
        with patch("claude_hooks.recall.run_recall", return_value=""):
            out = handle(
                event={"prompt": "anything", "cwd": "/tmp"},
                config=cfg, providers=[],
            )
        self.assertIsNotNone(out)
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("## Now", ac)

    def test_handler_emits_now_plus_recall(self):
        from claude_hooks.hooks.user_prompt_submit import handle
        cfg = {
            "hooks": {"user_prompt_submit": {"enabled": True, "min_prompt_chars": 0}},
            "system": {"now_block": {"enabled": True}},
        }
        with patch("claude_hooks.recall.run_recall",
                   return_value="## Recalled memory\n\n- A\n- B"):
            out = handle(
                event={"prompt": "anything", "cwd": "/tmp"},
                config=cfg, providers=[],
            )
        self.assertIsNotNone(out)
        ac = out["hookSpecificOutput"]["additionalContext"]
        # Now block is FIRST (top of context).
        self.assertTrue(ac.startswith("## Now"))
        # Recall follows.
        self.assertIn("## Recalled memory", ac)

    def test_handler_short_prompt_still_emits_now_block(self):
        """Even when the prompt is below min_chars and recall is
        skipped, the now-block should still surface — that's the
        whole point of making this an always-on injection."""
        from claude_hooks.hooks.user_prompt_submit import handle
        cfg = {
            "hooks": {"user_prompt_submit": {"enabled": True, "min_prompt_chars": 30}},
            "system": {"now_block": {"enabled": True}},
        }
        out = handle(
            event={"prompt": "hi", "cwd": "/tmp"},
            config=cfg, providers=[],
        )
        self.assertIsNotNone(out)
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("## Now", ac)
        self.assertNotIn("## Recalled memory", ac)

    def test_handler_disabled_now_block_falls_back_to_recall_only(self):
        from claude_hooks.hooks.user_prompt_submit import handle
        cfg = {
            "hooks": {"user_prompt_submit": {"enabled": True, "min_prompt_chars": 0}},
            "system": {"now_block": {"enabled": False}},
        }
        with patch("claude_hooks.recall.run_recall",
                   return_value="## Recalled memory\n\n- A"):
            out = handle(
                event={"prompt": "anything", "cwd": "/tmp"},
                config=cfg, providers=[],
            )
        self.assertIsNotNone(out)
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("## Now", ac)
        self.assertIn("## Recalled memory", ac)


if __name__ == "__main__":
    unittest.main()
