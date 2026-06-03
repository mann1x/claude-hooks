"""Tests for v1.6.0 install.py sqlite-vec MCP launcher path.

v1.6.0 extends ``_setup_sqlite_vec_mcp`` with a system-wide launcher
drop + ``~/.claude.json`` registration so external MCP clients (Cursor,
Codex, OpenWebUI, Claude Desktop) can share the same .db file the
hook framework uses in-process. Mirrors the pgvector-mcp path in
shape; covered here so the launcher / claude.json plumbing doesn't
regress.
"""

from __future__ import annotations

import io
import json
import os
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


# ----------------------------------------------------------------- #
# Launcher path resolver — platform-pinned
# ----------------------------------------------------------------- #


class TestLauncherPath(unittest.TestCase):

    def test_posix_path(self):
        if os.name != "posix":
            self.skipTest("POSIX-only")
        p = install._sqlite_vec_launcher_path()
        self.assertTrue(str(p).endswith("/.local/bin/sqlite-vec-mcp"),
                        f"unexpected POSIX path: {p}")

    def test_windows_path_source_pattern(self):
        # Can't instantiate WindowsPath on a POSIX host (pathlib's
        # ``Path.__new__`` resolves the flavour at construction). Pin
        # the behavior by source-inspecting the helper instead — same
        # technique the pgvector launcher tests use for cross-platform
        # path assertions.
        src = Path(install.__file__).read_text(encoding="utf-8")
        # Window branch must reference LOCALAPPDATA + the .cmd suffix.
        # We bracket the helper's body to keep the assertion tight.
        i = src.index("def _sqlite_vec_launcher_path")
        j = src.index("def ", i + 1)
        body = src[i:j]
        self.assertIn('os.name == "nt"', body)
        self.assertIn("LOCALAPPDATA", body)
        self.assertIn("sqlite-vec-mcp.cmd", body)
        self.assertIn("claude-hooks", body)


# ----------------------------------------------------------------- #
# Launcher writer — emits the right interpreter + module reference
# ----------------------------------------------------------------- #


class TestLauncherWriter(unittest.TestCase):

    def test_posix_writer_emits_shell_script_with_module(self):
        if os.name != "posix":
            self.skipTest("POSIX-only")
        with TemporaryDirectory() as d:
            target = Path(d) / "bin" / "sqlite-vec-mcp"
            install._write_sqlite_vec_launcher(
                target, py="/opt/py/bin/python", repo="/srv/repo",
            )
            body = target.read_text()
            self.assertIn("#!/usr/bin/env sh", body)
            self.assertIn("claude_hooks.sqlite_vec_mcp", body)
            self.assertIn("/opt/py/bin/python", body)
            self.assertIn("/srv/repo", body)
            # Identification tag — re-runs detect their own files.
            self.assertIn("sqlite-vec-mcp launcher (claude-hooks)", body)
            # Marked executable
            self.assertTrue(os.access(target, os.X_OK))

    def test_windows_writer_branch_pattern(self):
        # Same constraint as ``test_windows_path_source_pattern``: don't
        # try to instantiate a WindowsPath on a POSIX host. Pin the
        # branch via source inspection.
        src = Path(install.__file__).read_text(encoding="utf-8")
        i = src.index("def _write_sqlite_vec_launcher")
        j = src.index("def ", i + 1)
        body = src[i:j]
        self.assertIn('os.name == "nt"', body)
        self.assertIn("@echo off", body)
        self.assertIn("claude_hooks.sqlite_vec_mcp", body)
        self.assertIn("set PYTHONPATH", body)


# ----------------------------------------------------------------- #
# ~/.claude.json registration
# ----------------------------------------------------------------- #


class TestClaudeJsonRegistration(unittest.TestCase):

    def test_inserts_mcp_entry_pointing_at_launcher(self):
        with TemporaryDirectory() as d:
            home = Path(d)
            claude_json = home / ".claude.json"
            claude_json.write_text(json.dumps({"mcpServers": {
                "existing": {"type": "stdio", "command": "/keep/me"}
            }}))
            launcher = home / ".local" / "bin" / "sqlite-vec-mcp"
            # USERPROFILE, not HOME, is what os.path.expanduser("~") reads on
            # Windows — HOME-only isolation no-ops there, so the product would
            # write to the *real* ~/.claude.json (clobber hazard) and this temp
            # read would FileNotFoundError. Set both so isolation holds on both
            # platforms. See [[feedback_home_isolation_userprofile]].
            with patch.dict(install.os.environ,
                            {"HOME": str(home), "USERPROFILE": str(home)},
                            clear=False):
                install._register_sqlite_vec_mcp_in_claude_json(launcher)
            after = json.loads(claude_json.read_text())
            backups = [p.name for p in home.iterdir()
                       if p.name.startswith(".claude.json.bak-")]
        # New entry registered
        self.assertIn("sqlite_vec", after["mcpServers"])
        entry = after["mcpServers"]["sqlite_vec"]
        self.assertEqual(entry["type"], "stdio")
        self.assertEqual(entry["command"], str(launcher))
        # Existing entries preserved
        self.assertIn("existing", after["mcpServers"])
        # Backup file written with a sqlite-vec-mcp reason tag.
        self.assertTrue(any("sqlite-vec-mcp" in name for name in backups),
                        f"no semantic backup found: {backups}")

    def test_creates_claude_json_when_missing(self):
        with TemporaryDirectory() as d:
            home = Path(d)
            launcher = home / ".local" / "bin" / "sqlite-vec-mcp"
            # USERPROFILE, not HOME, is what os.path.expanduser("~") reads on
            # Windows — HOME-only isolation no-ops there, so the product would
            # write to the *real* ~/.claude.json (clobber hazard) and this temp
            # read would FileNotFoundError. Set both so isolation holds on both
            # platforms. See [[feedback_home_isolation_userprofile]].
            with patch.dict(install.os.environ,
                            {"HOME": str(home), "USERPROFILE": str(home)},
                            clear=False):
                install._register_sqlite_vec_mcp_in_claude_json(launcher)
            after = json.loads((home / ".claude.json").read_text())
        self.assertIn("sqlite_vec", after["mcpServers"])


# ----------------------------------------------------------------- #
# Non-interactive setup drops launcher + registers
# ----------------------------------------------------------------- #


class TestSetupNonInteractive(unittest.TestCase):

    def _base_cfg(self, *, already_enabled: bool = True) -> dict:
        return {"providers": {"sqlite_vec": {
            "enabled": already_enabled,
            "db_path": "~/.claude/claude-hooks-memory.db",
            "embedder": "llamafile",
            "embedder_options": {
                "url": "http://127.0.0.1:38092/embedding",
                "model": "qwen3-embedding:0.6b",
            },
        }}}

    def test_non_interactive_enabled_drops_launcher(self):
        out = io.StringIO()
        cfg = self._base_cfg(already_enabled=True)
        with patch.object(install, "_sqlite_vec_extension_available",
                          return_value=True), \
             patch.object(install, "_write_sqlite_vec_launcher") as drop, \
             patch.object(install, "_register_sqlite_vec_mcp_in_claude_json") as reg, \
             patch.object(install, "_setup_embedding_engine"), \
             patch.object(sys, "stdout", out):
            install._setup_sqlite_vec_mcp(
                cfg, non_interactive=True, dry_run=False,
            )
        drop.assert_called_once()
        reg.assert_called_once()

    def test_non_interactive_disabled_skips_everything(self):
        out = io.StringIO()
        cfg = self._base_cfg(already_enabled=False)
        with patch.object(install, "_write_sqlite_vec_launcher") as drop, \
             patch.object(install, "_register_sqlite_vec_mcp_in_claude_json") as reg, \
             patch.object(sys, "stdout", out):
            install._setup_sqlite_vec_mcp(
                cfg, non_interactive=True, dry_run=False,
            )
        drop.assert_not_called()
        reg.assert_not_called()
        self.assertIn("not currently enabled", out.getvalue())

    def test_dry_run_skips_writes(self):
        out = io.StringIO()
        cfg = self._base_cfg(already_enabled=True)
        with patch.object(install, "_sqlite_vec_extension_available",
                          return_value=True), \
             patch.object(install, "_write_sqlite_vec_launcher") as drop, \
             patch.object(install, "_register_sqlite_vec_mcp_in_claude_json") as reg, \
             patch.object(install, "_setup_embedding_engine"), \
             patch.object(sys, "stdout", out):
            install._setup_sqlite_vec_mcp(
                cfg, non_interactive=True, dry_run=True,
            )
        drop.assert_not_called()
        reg.assert_not_called()
        self.assertIn("[dry-run]", out.getvalue())


# ----------------------------------------------------------------- #
# Interactive setup happy + skip paths
# ----------------------------------------------------------------- #


class TestSetupInteractive(unittest.TestCase):

    def _empty_cfg(self) -> dict:
        return {"providers": {"sqlite_vec": {"enabled": False}}}

    def test_user_says_no_skips(self):
        out = io.StringIO()
        cfg = self._empty_cfg()
        with patch.object(install, "_write_sqlite_vec_launcher") as drop, \
             patch.object(install, "_register_sqlite_vec_mcp_in_claude_json") as reg, \
             patch("builtins.input", _scripted_input(["n"])), \
             patch.object(sys, "stdout", out):
            install._setup_sqlite_vec_mcp(
                cfg, non_interactive=False, dry_run=False,
            )
        drop.assert_not_called()
        reg.assert_not_called()
        self.assertIn("Skipped.", out.getvalue())

    def test_user_says_yes_drops_launcher(self):
        out = io.StringIO()
        cfg = self._empty_cfg()
        # Answers in order:
        #   "y"  -> enable sqlite_vec
        #   ""   -> default db_path
        #   "y"  -> install launcher (no existing launcher today)
        answers = ["y", "", "y"]
        with patch.object(install, "_sqlite_vec_extension_available",
                          return_value=True), \
             patch.object(install, "_write_sqlite_vec_launcher") as drop, \
             patch.object(install, "_register_sqlite_vec_mcp_in_claude_json") as reg, \
             patch.object(install, "_setup_embedding_engine"), \
             patch.object(install, "_sqlite_vec_launcher_path",
                          return_value=Path("/tmp/test-no-exist/sqlite-vec-mcp")), \
             patch("builtins.input", _scripted_input(answers)), \
             patch.object(sys, "stdout", out):
            install._setup_sqlite_vec_mcp(
                cfg, non_interactive=False, dry_run=False,
            )
        drop.assert_called_once()
        reg.assert_called_once()
        self.assertTrue(cfg["providers"]["sqlite_vec"]["enabled"])


# ----------------------------------------------------------------- #
# Pick-provider skip-list — sqlite_vec is handled by bespoke setup
# ----------------------------------------------------------------- #


class TestPickProviderSkipsSqliteVec(unittest.TestCase):
    """Regression guard for the dead 'Enter MCP URL for SQLite + sqlite-vec'
    prompt: with v1.6.0 the generic pick loop must skip sqlite_vec the same
    way it has always skipped pgvector. The bespoke ``_setup_sqlite_vec_mcp``
    owns the URL now.
    """

    def test_pick_provider_skip_list_includes_sqlite_vec(self):
        # The skip condition appears literally in install.py — assert on
        # the source so the next refactor can't lose the guard silently.
        src = Path(install.__file__).read_text(encoding="utf-8")
        self.assertIn('cls.name in ("pgvector", "sqlite_vec")', src)


if __name__ == "__main__":
    unittest.main()
