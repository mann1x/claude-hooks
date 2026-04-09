"""Tests for the dispatcher routing + the user_prompt_submit handler."""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks.config import DEFAULT_CONFIG
from claude_hooks.dispatcher import dispatch
from claude_hooks.providers.base import Memory, Provider, ServerCandidate


class FakeProvider(Provider):
    name = "fake"
    display_name = "Fake"

    @classmethod
    def signature_tools(cls):
        return set()

    @classmethod
    def detect(cls, claude_config):
        return []

    def recall(self, query, k=5):
        return [Memory(text=f"recalled: {query}", metadata={"k": k})]

    def store(self, content, metadata=None):
        self.last_stored = (content, metadata)


def make_fake(name="fake"):
    cand = ServerCandidate(server_key=name, url="http://fake")
    return FakeProvider(cand)


class TestDispatcher(unittest.TestCase):
    def test_unknown_event_no_op(self):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            rc = dispatch("NoSuchEvent", {})
        self.assertEqual(rc, 0)
        self.assertEqual(captured.getvalue(), "")

    def test_user_prompt_submit_injects_context(self):
        from copy import deepcopy
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["providers"]["qdrant"]["enabled"] = False
        cfg["providers"]["memory_kg"]["enabled"] = False
        # The fake provider name isn't in include_providers; allow all.
        cfg["hooks"]["user_prompt_submit"]["include_providers"] = None
        # Stub build_providers to return our fake.
        from claude_hooks import dispatcher as disp
        with patch.object(disp, "load_config", return_value=cfg), \
             patch.object(disp, "build_providers", return_value=[make_fake()]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = dispatch(
                    "UserPromptSubmit",
                    {"prompt": "tell me about bcache and friends", "session_id": "x", "cwd": "/tmp"},
                )
        self.assertEqual(rc, 0)
        out = captured.getvalue().strip()
        self.assertTrue(out, "expected stdout JSON output")
        parsed = json.loads(out)
        self.assertEqual(parsed["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("Recalled memory", parsed["hookSpecificOutput"]["additionalContext"])
        self.assertIn("recalled: tell me", parsed["hookSpecificOutput"]["additionalContext"])

    def test_user_prompt_submit_short_prompt_skipped(self):
        from copy import deepcopy
        cfg = deepcopy(DEFAULT_CONFIG)
        from claude_hooks import dispatcher as disp
        with patch.object(disp, "load_config", return_value=cfg), \
             patch.object(disp, "build_providers", return_value=[make_fake()]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = dispatch("UserPromptSubmit", {"prompt": "hi", "cwd": "/tmp"})
        self.assertEqual(rc, 0)
        self.assertEqual(captured.getvalue(), "")

    def test_handler_crash_does_not_block(self):
        class CrashingProvider(FakeProvider):
            def recall(self, query, k=5):
                raise RuntimeError("boom")

        from copy import deepcopy
        cfg = deepcopy(DEFAULT_CONFIG)
        cand = ServerCandidate(server_key="x", url="http://x")
        from claude_hooks import dispatcher as disp
        with patch.object(disp, "load_config", return_value=cfg), \
             patch.object(disp, "build_providers", return_value=[CrashingProvider(cand)]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = dispatch(
                    "UserPromptSubmit",
                    {"prompt": "this is a long enough prompt for the threshold", "cwd": "/tmp"},
                )
        # Should still exit 0, no output.
        self.assertEqual(rc, 0)

    def test_session_start_status_line(self):
        from copy import deepcopy
        cfg = deepcopy(DEFAULT_CONFIG)
        from claude_hooks import dispatcher as disp
        with patch.object(disp, "load_config", return_value=cfg), \
             patch.object(disp, "build_providers", return_value=[make_fake()]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = dispatch("SessionStart", {"source": "startup", "cwd": "/tmp"})
        self.assertEqual(rc, 0)
        out = captured.getvalue().strip()
        parsed = json.loads(out)
        self.assertIn("Started", parsed["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Fake", parsed["hookSpecificOutput"]["additionalContext"])

    def test_disable_marker_skips_dispatch(self):
        import tempfile
        from copy import deepcopy
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".claude-hooks-disable").touch()
            cfg = deepcopy(DEFAULT_CONFIG)
            from claude_hooks import dispatcher as disp
            with patch.object(disp, "load_config", return_value=cfg), \
                 patch.object(disp, "build_providers", return_value=[make_fake()]):
                captured = io.StringIO()
                with patch("sys.stdout", captured):
                    rc = dispatch(
                        "UserPromptSubmit",
                        {"prompt": "this is long enough to pass the threshold", "cwd": td},
                    )
            self.assertEqual(rc, 0)
            self.assertEqual(captured.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
