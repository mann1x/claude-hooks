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
        """Short prompts skip recall (the expensive provider fan-out)
        but still surface the always-on ## Now block — the model
        needs a fresh local-TZ timestamp every turn regardless of
        prompt length."""
        from copy import deepcopy
        cfg = deepcopy(DEFAULT_CONFIG)
        from claude_hooks import dispatcher as disp
        with patch.object(disp, "load_config", return_value=cfg), \
             patch.object(disp, "build_providers", return_value=[make_fake()]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = dispatch("UserPromptSubmit", {"prompt": "hi", "cwd": "/tmp"})
        self.assertEqual(rc, 0)
        out = captured.getvalue().strip()
        self.assertTrue(out, "now-block should still surface on short prompts")
        parsed = json.loads(out)
        ac = parsed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("## Now", ac)
        # Recall was skipped → no "Recalled memory" header.
        self.assertNotIn("## Recalled memory", ac)

    def test_user_prompt_submit_short_prompt_no_now_block_returns_none(self):
        """When the now-block is disabled AND recall is skipped,
        the handler must return None (no spurious output)."""
        from copy import deepcopy
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg.setdefault("system", {}).setdefault("now_block", {})["enabled"] = False
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


class TestDispatchCaptureThreadSafety(unittest.TestCase):
    """Regression tests for the daemon stdout-race bug.

    Background: when the daemon was multi-threaded, two concurrent
    dispatches both clobbered ``sys.stdout`` to redirect into per-call
    StringIO buffers. The redirects raced — one thread's handler output
    landed in another thread's buffer, and a Stop hook would receive a
    UserPromptSubmit recall payload (rejected by Claude Code with
    "Hook returned incorrect event name").

    The fix is :func:`dispatcher.dispatch_capture` — returns the
    output dict directly, never touches global stdout. These tests
    pin that contract so a future refactor can't reintroduce the
    shared-stdout pattern.
    """

    def test_dispatch_capture_returns_output_without_touching_stdout(self):
        from copy import deepcopy
        from claude_hooks.dispatcher import dispatch_capture
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["providers"]["qdrant"]["enabled"] = False
        cfg["providers"]["memory_kg"]["enabled"] = False
        cfg["hooks"]["user_prompt_submit"]["include_providers"] = None

        from claude_hooks import dispatcher as disp
        sentinel = io.StringIO()
        sentinel.write("UNTOUCHED")
        with patch.object(disp, "load_config", return_value=cfg), \
             patch.object(disp, "build_providers", return_value=[make_fake()]), \
             patch("sys.stdout", sentinel):
            out = dispatch_capture(
                "UserPromptSubmit",
                {"prompt": "tell me about bcache and friends",
                 "session_id": "x", "cwd": "/tmp"},
            )

        self.assertIsInstance(out, dict)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        # Critical: stdout MUST NOT have been written to.
        self.assertEqual(sentinel.getvalue(), "UNTOUCHED",
                         "dispatch_capture must not touch sys.stdout")

    def test_concurrent_dispatch_capture_does_not_cross_streams(self):
        """Run UserPromptSubmit + Stop concurrently; assert each thread
        gets back the right event-typed output.

        Prior to the fix, this test failed roughly 1 in 5 runs because
        the user_prompt_submit handler's stdout write landed in the
        Stop thread's buffer.
        """
        import threading
        from copy import deepcopy
        from claude_hooks.dispatcher import dispatch_capture

        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["providers"]["qdrant"]["enabled"] = False
        cfg["providers"]["memory_kg"]["enabled"] = False
        cfg["hooks"]["user_prompt_submit"]["include_providers"] = None

        from claude_hooks import dispatcher as disp

        results: dict[str, dict] = {}

        def run(event_name: str, event: dict, key: str):
            with patch.object(disp, "load_config", return_value=deepcopy(cfg)), \
                 patch.object(disp, "build_providers", return_value=[make_fake()]):
                out = dispatch_capture(event_name, event)
            results[key] = out

        for _ in range(20):
            results.clear()
            t1 = threading.Thread(target=run, args=(
                "UserPromptSubmit",
                {"prompt": "tell me about bcache and friends",
                 "session_id": "a", "cwd": "/tmp"},
                "ups",
            ))
            t2 = threading.Thread(target=run, args=(
                "Stop",
                {"session_id": "b", "cwd": "/tmp",
                 "transcript_path": "/nonexistent/trans.jsonl"},
                "stop",
            ))
            t1.start(); t2.start()
            t1.join(); t2.join()

            ups = results.get("ups")
            stop = results.get("stop")
            # UserPromptSubmit output, if any, must carry its own event name.
            if ups is not None:
                hso = ups.get("hookSpecificOutput") or {}
                self.assertEqual(
                    hso.get("hookEventName", "UserPromptSubmit"),
                    "UserPromptSubmit",
                    f"UPS thread got wrong event name: {hso}",
                )
            # Stop output must NOT carry hookEventName=UserPromptSubmit.
            if stop is not None:
                hso = stop.get("hookSpecificOutput") or {}
                self.assertNotEqual(
                    hso.get("hookEventName"), "UserPromptSubmit",
                    f"Stop thread received UPS payload — race regression: {stop}",
                )


if __name__ == "__main__":
    unittest.main()
