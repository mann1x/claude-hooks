"""Tests for the rtk command rewriter."""

import unittest
from unittest.mock import MagicMock, patch

from claude_hooks import rtk_rewrite
from claude_hooks.rtk_rewrite import (
    _parse_version,
    build_rewrite_response,
    rewrite_command,
)


def _mock_completed(stdout="", returncode=0):
    cp = MagicMock()
    cp.stdout = stdout
    cp.stderr = ""
    cp.returncode = returncode
    return cp


class ParseVersionTests(unittest.TestCase):
    def test_parses_standard_output(self):
        self.assertEqual(_parse_version("rtk 0.36.0"), (0, 36, 0))

    def test_parses_with_prefix(self):
        self.assertEqual(_parse_version("rtk version 1.2.3 (build x)"), (1, 2, 3))

    def test_no_version_returns_none(self):
        self.assertIsNone(_parse_version("some garbage"))


class RewriteCommandTests(unittest.TestCase):
    def setUp(self):
        rtk_rewrite.reset_rtk_cache()

    def tearDown(self):
        rtk_rewrite.reset_rtk_cache()

    def test_no_binary_returns_none(self):
        with patch("claude_hooks.rtk_rewrite.shutil.which", return_value=None):
            self.assertIsNone(rewrite_command("find . -name '*.ts'"))

    def test_old_version_returns_none(self):
        with patch("claude_hooks.rtk_rewrite.shutil.which", return_value="/usr/bin/rtk"), \
             patch(
                "claude_hooks.rtk_rewrite.subprocess.run",
                return_value=_mock_completed(stdout="rtk 0.1.0"),
             ):
            self.assertIsNone(rewrite_command("find . -name '*.ts'"))

    def test_rewrite_succeeds(self):
        # First subprocess.run is the version probe; second is the rewrite.
        calls = [
            _mock_completed(stdout="rtk 0.36.0"),       # version probe
            _mock_completed(stdout="rtk find ts"),       # rewrite
        ]
        with patch("claude_hooks.rtk_rewrite.shutil.which", return_value="/usr/bin/rtk"), \
             patch("claude_hooks.rtk_rewrite.subprocess.run", side_effect=calls):
            result = rewrite_command("find . -name '*.ts'")
            self.assertEqual(result, "rtk find ts")

    def test_rewrite_unchanged_returns_none(self):
        calls = [
            _mock_completed(stdout="rtk 0.36.0"),
            _mock_completed(stdout="ls -la"),   # unchanged
        ]
        with patch("claude_hooks.rtk_rewrite.shutil.which", return_value="/usr/bin/rtk"), \
             patch("claude_hooks.rtk_rewrite.subprocess.run", side_effect=calls):
            self.assertIsNone(rewrite_command("ls -la"))

    def test_rewrite_nonzero_returns_none(self):
        # rtk exits 1 when no rewrite is known — treat as pass-through.
        calls = [
            _mock_completed(stdout="rtk 0.36.0"),
            _mock_completed(stdout="", returncode=1),
        ]
        with patch("claude_hooks.rtk_rewrite.shutil.which", return_value="/usr/bin/rtk"), \
             patch("claude_hooks.rtk_rewrite.subprocess.run", side_effect=calls):
            self.assertIsNone(rewrite_command("some cmd"))

    def test_rewrite_timeout_returns_none(self):
        import subprocess
        with patch("claude_hooks.rtk_rewrite.shutil.which", return_value="/usr/bin/rtk"), \
             patch(
                "claude_hooks.rtk_rewrite.subprocess.run",
                side_effect=[
                    _mock_completed(stdout="rtk 0.36.0"),
                    subprocess.TimeoutExpired(cmd="rtk", timeout=3),
                ],
             ):
            self.assertIsNone(rewrite_command("slow cmd"))

    def test_empty_command(self):
        self.assertIsNone(rewrite_command(""))
        self.assertIsNone(rewrite_command("   "))

    def test_cache_reuses_version(self):
        # Two calls — version probe should happen only once.
        calls = [
            _mock_completed(stdout="rtk 0.36.0"),   # probe (once)
            _mock_completed(stdout="rewritten1"),
            _mock_completed(stdout="rewritten2"),
        ]
        with patch("claude_hooks.rtk_rewrite.shutil.which", return_value="/usr/bin/rtk"), \
             patch(
                "claude_hooks.rtk_rewrite.subprocess.run", side_effect=calls,
             ) as mock_run:
            self.assertEqual(rewrite_command("find . -name '*.ts'"), "rewritten1")
            self.assertEqual(rewrite_command("grep foo"), "rewritten2")
            self.assertEqual(mock_run.call_count, 3)  # probe + 2 rewrites


class ResponseShapeTests(unittest.TestCase):
    def test_build_rewrite_response(self):
        r = build_rewrite_response({"command": "old", "description": "d"}, "new")
        h = r["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        self.assertEqual(h["permissionDecision"], "allow")
        self.assertEqual(h["updatedInput"]["command"], "new")
        # Other keys preserved.
        self.assertEqual(h["updatedInput"]["description"], "d")


if __name__ == "__main__":
    unittest.main()
