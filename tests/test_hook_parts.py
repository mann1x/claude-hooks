"""``.claude-hooks-disable`` can keep named parts: memory and mailbox.

The property that makes this safe for development is that a project
with a marker never reaches the normal handlers — only the restricted
path, which can call memory and mailbox functions and nothing else.
"""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from claude_hooks import hook_parts
from claude_hooks.config import DEFAULT_CONFIG

MARKER = ".claude-hooks-disable"


class ParseMarkerTests(unittest.TestCase):

    def test_an_empty_marker_keeps_nothing(self):
        self.assertEqual(hook_parts.parse_marker(""), frozenset())
        self.assertEqual(hook_parts.parse_marker("# only a comment\n\n"),
                         frozenset())

    def test_the_keep_forms(self):
        for text in ("keep: memory, mailbox", "keep = memory mailbox",
                     "memory\nmailbox", "KEEP: Memory,Mailbox  # dev"):
            self.assertEqual(hook_parts.parse_marker(text),
                             frozenset({"memory", "mailbox"}), text)

    def test_one_part(self):
        self.assertEqual(hook_parts.parse_marker("keep: mailbox"),
                         frozenset({"mailbox"}))

    def test_an_unknown_name_enables_nothing_and_spares_the_rest(self):
        with self.assertLogs("claude_hooks.hook_parts", "WARNING"):
            kept = hook_parts.parse_marker("keep: memory, code_graph, lsp")
        self.assertEqual(kept, frozenset({"memory"}))


class FindMarkerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_marker_means_no_opinion(self):
        self.assertIsNone(hook_parts.kept_parts(str(self.root), MARKER))

    def test_a_subdirectory_inherits(self):
        (self.root / MARKER).write_text("keep: mailbox\n")
        sub = self.root / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(hook_parts.kept_parts(str(sub), MARKER),
                         frozenset({"mailbox"}))

    def test_the_nearest_marker_wins(self):
        (self.root / MARKER).write_text("keep: memory, mailbox\n")
        sub = self.root / "sub"
        sub.mkdir()
        (sub / MARKER).touch()
        self.assertEqual(hook_parts.kept_parts(str(sub), MARKER), frozenset())


class DispatcherRoutingTests(unittest.TestCase):
    """With a marker that keeps parts, the normal handler never runs."""

    def _dispatch(self, marker_text, event_name, extra=None):
        from claude_hooks import dispatcher as disp
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / MARKER).write_text(marker_text)
            event = {"cwd": td, "session_id": "s1",
                     "prompt": "long enough to clear the recall threshold"}
            event.update(extra or {})
            with patch.object(disp, "load_config",
                              return_value=deepcopy(DEFAULT_CONFIG)), \
                 patch.object(disp, "build_providers", return_value=[]), \
                 patch.object(hook_parts, "run",
                              return_value=None) as run, \
                 patch("builtins.__import__",
                       side_effect=self._guard_import(disp)):
                disp.dispatch_capture(event_name, event)
            return run

    @staticmethod
    def _guard_import(disp):
        real = __import__

        def guarded(name, *args, **kwargs):
            if name.startswith("claude_hooks.hooks."):
                raise AssertionError(f"normal handler imported: {name}")
            return real(name, *args, **kwargs)
        return guarded

    def test_a_keeping_marker_routes_to_the_restricted_path(self):
        run = self._dispatch("keep: mailbox\n", "UserPromptSubmit")
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["keep"], frozenset({"mailbox"}))

    def test_tool_events_reach_no_normal_handler(self):
        run = self._dispatch("keep: memory, mailbox\n", "PostToolUse",
                             {"tool_name": "Edit"})
        run.assert_called_once()

    def test_an_empty_marker_still_turns_everything_off(self):
        run = self._dispatch("", "UserPromptSubmit")
        run.assert_not_called()


class RestrictedHandlersTests(unittest.TestCase):

    def setUp(self):
        self.cfg = deepcopy(DEFAULT_CONFIG)
        self.event = {"cwd": "/x", "session_id": "s1",
                      "prompt": "long enough to clear the recall threshold"}

    def test_mailbox_only_does_not_recall(self):
        with patch("claude_hooks.recall.run_recall") as recall, \
             patch("claude_hooks.mailbox.hook.announce_block",
                   return_value="## Messages\n- #1") as announce:
            out = hook_parts.run("UserPromptSubmit", event=self.event,
                                 config=self.cfg, providers=[],
                                 keep=frozenset({"mailbox"}))
        recall.assert_not_called()
        announce.assert_called_once()
        self.assertIn("## Messages",
                      out["hookSpecificOutput"]["additionalContext"])

    def test_memory_only_does_not_touch_the_mailbox(self):
        with patch("claude_hooks.recall.run_recall",
                   return_value="## Recalled memory") as recall, \
             patch("claude_hooks.mailbox.hook.announce_block") as announce:
            hook_parts.run("UserPromptSubmit", event=self.event,
                           config=self.cfg, providers=[],
                           keep=frozenset({"memory"}))
        recall.assert_called_once()
        announce.assert_not_called()

    def test_a_resumed_session_re_registers(self):
        with patch("claude_hooks.mailbox.hook.register_session",
                   return_value="") as register, \
             patch("claude_hooks.mailbox.hook.announce_block",
                   return_value=""):
            hook_parts.run("SessionStart",
                           event={**self.event, "source": "compact"},
                           config=self.cfg, providers=[],
                           keep=frozenset({"mailbox"}))
        register.assert_called_once()

    def test_stop_stores_through_store_turn_only(self):
        with patch("claude_hooks.hooks.stop.store_turn",
                   return_value="[claude-hooks] stored to pgvector") as store, \
             patch("claude_hooks.hooks.stop._mailbox_notice",
                   return_value="") as notice:
            out = hook_parts.run("Stop", event=self.event, config=self.cfg,
                                 providers=[], keep=frozenset({"memory"}))
        store.assert_called_once()
        notice.assert_not_called()
        self.assertIn("stored to pgvector", out["systemMessage"])

    def test_tool_events_do_nothing(self):
        for name in ("PreToolUse", "PostToolUse", "PreCompact"):
            self.assertIsNone(hook_parts.run(
                name, event=self.event, config=self.cfg, providers=[],
                keep=frozenset({"memory", "mailbox"})))

    def test_a_failing_part_soft_fails(self):
        with patch("claude_hooks.mailbox.hook.announce_block",
                   side_effect=RuntimeError("db gone")):
            self.assertIsNone(hook_parts.run(
                "UserPromptSubmit", event=self.event, config=self.cfg,
                providers=[], keep=frozenset({"mailbox"})))


if __name__ == "__main__":
    unittest.main()
