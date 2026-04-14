"""Handler-level tests for hooks/pre_tool_use.py.

These exercise the wiring between the rtk_rewrite, safety_scan, and
memory-warn stages — orthogonal to the unit tests on the underlying
library modules.
"""

from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch

from claude_hooks import rtk_rewrite, safety_scan, stop_guard
from claude_hooks.config import DEFAULT_CONFIG
from claude_hooks.hooks.pre_tool_use import handle


def _fresh_config() -> dict:
    cfg = deepcopy(DEFAULT_CONFIG)
    # Keep the log off for tests — don't touch ~/.claude/permission-scanner/
    cfg["hooks"]["pre_tool_use"]["safety_log_enabled"] = False
    return cfg


class PreToolUseOrderingTests(unittest.TestCase):
    """Verify the stage-ordering invariants documented in the handler."""

    def setUp(self):
        rtk_rewrite.reset_rtk_cache()
        safety_scan.reset_pattern_cache()

    def tearDown(self):
        rtk_rewrite.reset_rtk_cache()
        safety_scan.reset_pattern_cache()

    def test_all_stages_off_returns_none(self):
        cfg = _fresh_config()
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["enabled"] = False
        r = handle(
            event={"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
            config=cfg,
            providers=[],
        )
        self.assertIsNone(r)

    def test_safety_scan_alone_catches_dangerous(self):
        cfg = _fresh_config()
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = True
        r = handle(
            event={"tool_name": "Bash", "tool_input": {"command": "sudo reboot"}},
            config=cfg,
            providers=[],
        )
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("sudo", r["hookSpecificOutput"]["permissionDecisionReason"])

    def test_safety_scan_alone_passes_safe_command(self):
        cfg = _fresh_config()
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = True
        r = handle(
            event={"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
            config=cfg,
            providers=[],
        )
        self.assertIsNone(r)

    def test_rtk_alone_still_triggers_safety_on_rewrite(self):
        """M1: with rtk on / safety_scan off, rewrites must still be scanned.

        Prevents the allow-list bypass: when we emit permissionDecision
        for a rewrite, settings.json rules don't re-apply, so we must
        provide the safety net ourselves.
        """
        cfg = _fresh_config()
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = True
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = False
        dangerous_rewrite = "rtk ls && rm -rf /tmp/foo"
        with patch(
            "claude_hooks.hooks.pre_tool_use._run_rtk_rewrite_raw",
            return_value=dangerous_rewrite,
        ):
            r = handle(
                event={
                    "tool_name": "Bash",
                    "tool_input": {"command": "ls && rm -rf /tmp/foo"},
                },
                config=cfg,
                providers=[],
            )
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("rm -rf", r["hookSpecificOutput"]["permissionDecisionReason"])
        # The rewritten form should surface to the user so they can see
        # what would actually run if they approve.
        self.assertEqual(
            r["hookSpecificOutput"]["updatedInput"]["command"], dangerous_rewrite
        )

    def test_rtk_scan_rewrites_false_disables_safety_on_rewrite(self):
        """Opt-out: rtk_scan_rewrites=false lets dangerous rewrites through."""
        cfg = _fresh_config()
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = True
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["rtk_scan_rewrites"] = False
        with patch(
            "claude_hooks.hooks.pre_tool_use._run_rtk_rewrite_raw",
            return_value="rtk ls && rm -rf /tmp/foo",
        ):
            r = handle(
                event={
                    "tool_name": "Bash",
                    "tool_input": {"command": "ls && rm -rf /tmp/foo"},
                },
                config=cfg,
                providers=[],
            )
        # User explicitly opted out — rewrite is auto-approved.
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_rtk_alone_allows_safe_rewrite(self):
        cfg = _fresh_config()
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = True
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = False
        with patch(
            "claude_hooks.hooks.pre_tool_use._run_rtk_rewrite_raw",
            return_value="rtk find py",
        ):
            r = handle(
                event={
                    "tool_name": "Bash",
                    "tool_input": {"command": "find . -name '*.py'"},
                },
                config=cfg,
                providers=[],
            )
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(
            r["hookSpecificOutput"]["updatedInput"]["command"], "rtk find py"
        )

    def test_rtk_no_rewrite_safety_off_passes_through(self):
        """rtk finds nothing to rewrite AND safety_scan is off → None."""
        cfg = _fresh_config()
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = True
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = False
        with patch(
            "claude_hooks.hooks.pre_tool_use._run_rtk_rewrite_raw",
            return_value=None,
        ):
            r = handle(
                event={
                    "tool_name": "Bash",
                    "tool_input": {"command": "sudo reboot"},  # dangerous but not rewritten
                },
                config=cfg,
                providers=[],
            )
        self.assertIsNone(
            r,
            "safety_scan is OFF and rtk didn't rewrite — settings.json allow-list "
            "should see the raw command unimpeded",
        )

    def test_non_bash_tool_ignored_by_rtk_and_scan(self):
        cfg = _fresh_config()
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = True
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = True
        r = handle(
            event={
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "/etc/passwd",  # looks scary but we don't scan Edit paths
                    "old_string": "x",
                    "new_string": "y",
                },
            },
            config=cfg,
            providers=[],
        )
        self.assertIsNone(r)


class StopGuardMetaContextHandlerTests(unittest.TestCase):
    """Integration: stop_guard inside the Stop handler honours meta-context escape."""

    def setUp(self):
        stop_guard.reset_pattern_cache()

    def tearDown(self):
        stop_guard.reset_pattern_cache()

    def test_guard_disabled_returns_none(self):
        from claude_hooks.hooks.stop import _run_stop_guard
        transcript = [
            {"role": "assistant", "content": "pre-existing issue here"},
        ]
        # guard_cfg enabled=False → _run_stop_guard still called only if enabled
        # but we test the helper directly
        result = _run_stop_guard(transcript, guard_cfg={"enabled": False})
        # _run_stop_guard doesn't read "enabled" (that's the caller's job),
        # so this directly exercises the pattern check. Confirm it matches.
        self.assertIsNotNone(result)

    def test_guard_skips_quoted_example(self):
        from claude_hooks.hooks.stop import _run_stop_guard
        transcript = [{
            "role": "assistant",
            "content": (
                'For example, the trigger phrase "pre-existing issue" '
                "would trigger the stop_guard rule."
            ),
        }]
        # With skip_meta_context=True (default), this is a quoted example
        # AND contains meta-markers → should NOT fire.
        result = _run_stop_guard(transcript, guard_cfg={})
        self.assertIsNone(result)

    def test_guard_fires_on_real_dodge(self):
        from claude_hooks.hooks.stop import _run_stop_guard
        transcript = [{
            "role": "assistant",
            "content": "The failing test is a pre-existing issue, not my concern.",
        }]
        result = _run_stop_guard(transcript, guard_cfg={})
        self.assertIsNotNone(result)
        self.assertIn("NOTHING IS PRE-EXISTING", result)


if __name__ == "__main__":
    unittest.main()
