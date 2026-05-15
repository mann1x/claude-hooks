"""Tests for v1.5.1 install.py path-drift safeguard + backup helper.

Reproduces the failure mode that silently broke pandorum's install
on 2026-05-12: someone ran ``python install.py`` from a second clone
checked out at a different path, and the installer rewrote every
``_managedBy: claude-hooks`` hook entry to point at the new location
without warning, effectively un-deploying the working install.

Covers:
- ``_extract_existing_hook_repo_path`` correctly identifies the
  repo path embedded in existing managed entries.
- ``install_hooks`` refuses to rewrite (``HookPathDrift`` with rc=2)
  when ``--non-interactive`` is set and the existing hooks point at
  a different repo path.
- Interactive mode prompts and respects ``n`` (no rewrite).
- ``--rewire`` overrides the refusal in non-interactive mode.
- Idempotent path match (same repo, re-run) is NOT flagged as drift.
- ``_backed_up_save_json`` writes a timestamped semantic backup
  before mutating the target file.
- ``backup_path`` includes the ``reason`` tag in the filename.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


def _scripted_input(answers):
    it = iter(answers)

    def _fn(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(
                f"dialog asked an unscripted question: {prompt!r}"
            )
    return _fn


def _settings_with_hooks(repo_path: str) -> dict:
    """Build a minimal settings.json with claude-hooks managed entries
    pointing at ``repo_path``."""
    return {
        "hooks": {
            "UserPromptSubmit": [{
                "_managedBy": "claude-hooks",
                "hooks": [{
                    "type": "command",
                    "command": f"{repo_path}/bin/claude-hook UserPromptSubmit",
                    "timeout": 15,
                }],
            }],
            "SessionStart": [{
                "_managedBy": "claude-hooks",
                "hooks": [{
                    "type": "command",
                    "command": f"{repo_path}/bin/claude-hook SessionStart",
                    "timeout": 5,
                }],
            }],
        }
    }


# ----------------------------------------------------------------- #
# backup_path + _backed_up_save_json
# ----------------------------------------------------------------- #

class TestBackupHelpers(unittest.TestCase):

    def test_backup_path_includes_reason_tag(self):
        p = Path("/tmp/settings.json")
        bak = install.backup_path(p, reason="hook-rewrite")
        # .json + .bak-YYYYMMDD-HHMMSS-hook-rewrite
        self.assertTrue(bak.name.endswith("-hook-rewrite"))
        self.assertIn(".bak-", bak.name)
        self.assertTrue(bak.name.startswith("settings.json.bak-"))

    def test_backup_path_default_reason(self):
        p = Path("/tmp/settings.json")
        bak = install.backup_path(p)
        self.assertTrue(bak.name.endswith("-save"))

    def test_backup_path_sanitizes_reason(self):
        p = Path("/tmp/settings.json")
        # Reason with invalid chars should be sanitized to hyphens
        bak = install.backup_path(p, reason="rewrite from /A/B/C")
        # Only alphanumerics + hyphens + underscores allowed
        suffix = bak.name.split(".bak-", 1)[1]
        # After timestamp + dash, the rest is the reason
        reason_part = suffix.split("-", 2)[2]
        self.assertTrue(all(c.isalnum() or c in "-_" for c in reason_part))

    def test_backed_up_save_writes_backup_then_data(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            p.write_text(json.dumps({"version": 1}), encoding="utf-8")
            bak = install._backed_up_save_json(
                p, {"version": 2}, reason="test",
            )
            # Backup written + named with reason
            self.assertIsNotNone(bak)
            self.assertTrue(bak.exists())
            self.assertIn("-test", bak.name)
            # Backup contains the OLD content
            self.assertEqual(json.loads(bak.read_text()), {"version": 1})
            # File contains the NEW content
            self.assertEqual(json.loads(p.read_text()), {"version": 2})

    def test_backed_up_save_no_backup_when_fresh_file(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            # File doesn't exist yet
            bak = install._backed_up_save_json(
                p, {"version": 1}, reason="test",
            )
            self.assertIsNone(bak)
            self.assertTrue(p.exists())
            self.assertEqual(json.loads(p.read_text()), {"version": 1})

    def test_backed_up_save_dry_run_skips(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            p.write_text(json.dumps({"v": 1}), encoding="utf-8")
            bak = install._backed_up_save_json(
                p, {"v": 2}, reason="test", dry_run=True,
            )
            self.assertIsNone(bak)
            # File unchanged
            self.assertEqual(json.loads(p.read_text()), {"v": 1})


# ----------------------------------------------------------------- #
# _extract_existing_hook_repo_path
# ----------------------------------------------------------------- #

class TestExtractRepoPath(unittest.TestCase):

    def test_empty_settings_returns_none(self):
        self.assertIsNone(install._extract_existing_hook_repo_path({}))

    def test_no_managed_entries_returns_none(self):
        s = {"hooks": {"UserPromptSubmit": [{
            "hooks": [{"command": "/some/other/hook foo"}],
        }]}}
        self.assertIsNone(install._extract_existing_hook_repo_path(s))

    def test_extracts_posix_path(self):
        s = _settings_with_hooks("/srv/dev/claude-hooks")
        repo = install._extract_existing_hook_repo_path(s)
        self.assertEqual(repo, Path("/srv/dev/claude-hooks"))

    def test_extracts_windows_path(self):
        # Real-world from pandorum's settings.json
        s = _settings_with_hooks("C:/Users/manni/claude-hooks")
        repo = install._extract_existing_hook_repo_path(s)
        self.assertEqual(repo, Path("C:/Users/manni/claude-hooks"))

    def test_extracts_windows_backslash_command(self):
        # Some installers write backslash commands
        s = {"hooks": {"X": [{
            "_managedBy": "claude-hooks",
            "hooks": [{
                "command": r"C:\Users\manni\claude-hooks\bin\claude-hook.cmd X",
            }],
        }]}}
        repo = install._extract_existing_hook_repo_path(s)
        self.assertEqual(repo, Path("C:/Users/manni/claude-hooks"))

    def test_multiple_paths_returns_most_common(self):
        s = {"hooks": {
            "A": [{"_managedBy": "claude-hooks", "hooks": [
                {"command": "/path/A/bin/claude-hook A"},
            ]}],
            "B": [{"_managedBy": "claude-hooks", "hooks": [
                {"command": "/path/A/bin/claude-hook B"},
                {"command": "/path/B/bin/claude-hook B2"},
            ]}],
        }}
        # /path/A appears 2x, /path/B appears 1x
        self.assertEqual(
            install._extract_existing_hook_repo_path(s),
            Path("/path/A"),
        )


# ----------------------------------------------------------------- #
# install_hooks drift detection
# ----------------------------------------------------------------- #

class TestInstallHooksDrift(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.settings_path = self.tmp_path / "settings.json"
        # Pre-seed with hooks pointing at PATH_A
        self.path_a = "C:/Users/manni/claude-hooks"
        self.path_b = Path("C:/Users/manni/dev/claude-hooks")
        self.settings_path.write_text(
            json.dumps(_settings_with_hooks(self.path_a)),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_interactive_drift_raises_systemexit_2(self):
        with self.assertRaises(install.HookPathDrift) as ctx:
            install.install_hooks(
                self.settings_path,
                repo_path=self.path_b,
                include_pre_tool_use=False,
                include_post_tool_use=False,
                include_pre_compact=False,
                dry_run=False,
                non_interactive=True,
                rewire=False,
            )
        self.assertEqual(ctx.exception.code, 2)
        # File unchanged
        s = json.loads(self.settings_path.read_text())
        first_cmd = s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn(self.path_a, first_cmd)

    def test_non_interactive_with_rewire_overrides(self):
        # Even with non_interactive=True, rewire=True must succeed
        install.install_hooks(
            self.settings_path,
            repo_path=self.path_b,
            include_pre_tool_use=False,
            include_post_tool_use=False,
            include_pre_compact=False,
            dry_run=False,
            non_interactive=True,
            rewire=True,
        )
        s = json.loads(self.settings_path.read_text())
        first_cmd = s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn("dev/claude-hooks", first_cmd.replace("\\", "/"))

    def test_interactive_drift_prompt_no_skips_rewrite(self):
        with patch("builtins.input", _scripted_input(["n"])):
            install.install_hooks(
                self.settings_path,
                repo_path=self.path_b,
                include_pre_tool_use=False,
                include_post_tool_use=False,
                include_pre_compact=False,
                dry_run=False,
                non_interactive=False,
            )
        # File unchanged
        s = json.loads(self.settings_path.read_text())
        first_cmd = s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn(self.path_a, first_cmd)

    def test_interactive_drift_prompt_yes_rewrites(self):
        with patch("builtins.input", _scripted_input(["y"])):
            install.install_hooks(
                self.settings_path,
                repo_path=self.path_b,
                include_pre_tool_use=False,
                include_post_tool_use=False,
                include_pre_compact=False,
                dry_run=False,
                non_interactive=False,
            )
        s = json.loads(self.settings_path.read_text())
        first_cmd = s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn("dev/claude-hooks", first_cmd.replace("\\", "/"))

    def test_same_path_no_drift_silent_rewrite(self):
        # Re-run from the same path: no prompt, idempotent rewrite
        install.install_hooks(
            self.settings_path,
            repo_path=Path(self.path_a),
            include_pre_tool_use=False,
            include_post_tool_use=False,
            include_pre_compact=False,
            dry_run=False,
            non_interactive=True,  # would refuse if drift detected
        )
        # Still pointing at path_a, no error
        s = json.loads(self.settings_path.read_text())
        first_cmd = s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn(self.path_a, first_cmd)

    def test_fresh_install_no_existing_hooks_no_drift(self):
        # No prior entries: not drift, just install
        self.settings_path.write_text("{}", encoding="utf-8")
        install.install_hooks(
            self.settings_path,
            repo_path=self.path_b,
            include_pre_tool_use=False,
            include_post_tool_use=False,
            include_pre_compact=False,
            dry_run=False,
            non_interactive=True,
        )
        s = json.loads(self.settings_path.read_text())
        self.assertIn("hooks", s)
        self.assertIn("UserPromptSubmit", s["hooks"])

    def test_drift_rewrite_creates_semantic_backup(self):
        # After a drift rewrite, backup should be named *-hook-rewrite
        install.install_hooks(
            self.settings_path,
            repo_path=self.path_b,
            include_pre_tool_use=False,
            include_post_tool_use=False,
            include_pre_compact=False,
            dry_run=False,
            non_interactive=True,
            rewire=True,
        )
        baks = list(self.tmp_path.glob("settings.json.bak-*"))
        self.assertEqual(len(baks), 1)
        self.assertIn("-hook-rewrite", baks[0].name)
        # Backup contains the OLD path
        old_content = json.loads(baks[0].read_text())
        old_cmd = old_content["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn(self.path_a, old_cmd)


if __name__ == "__main__":
    unittest.main()
