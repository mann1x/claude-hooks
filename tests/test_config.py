"""Tests for config load/save and project disable marker."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks import config as config_mod
from claude_hooks.config import (
    DEFAULT_CONFIG,
    _audit_log_config_write,
    expand_user_path,
    load_config,
    project_disabled,
    save_config,
)


class TestConfig(unittest.TestCase):
    def setUp(self):
        # v1.10.2: prevent test runs from polluting the real user audit
        # log at ~/.claude/claude-hooks-config-writes.log. save_config
        # now appends an audit line on every write; mocking the log
        # path to a per-test tmp dir keeps the real one untouched.
        self._audit_tmp = tempfile.TemporaryDirectory()
        self._audit_patch = patch.object(
            config_mod,
            "CONFIG_WRITE_AUDIT_LOG",
            Path(self._audit_tmp.name) / "audit.log",
        )
        self._audit_patch.start()
        self.addCleanup(self._audit_patch.stop)
        self.addCleanup(self._audit_tmp.cleanup)

    def test_load_missing_returns_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = load_config(Path(td) / "missing.json")
            self.assertEqual(cfg["version"], DEFAULT_CONFIG["version"])
            self.assertIn("qdrant", cfg["providers"])
            self.assertIn("memory_kg", cfg["providers"])

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude-hooks.json"
            cfg = {
                "version": 1,
                "providers": {
                    "qdrant": {"enabled": True, "mcp_url": "http://x/mcp", "collection": "memory"}
                },
            }
            save_config(cfg, path)
            self.assertTrue(path.exists())
            loaded = load_config(path)
            self.assertEqual(loaded["providers"]["qdrant"]["mcp_url"], "http://x/mcp")
            # Defaults should still be merged in
            self.assertIn("memory_kg", loaded["providers"])

    def test_user_config_overrides_default(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude-hooks.json"
            with open(path, "w") as f:
                json.dump({"providers": {"qdrant": {"recall_k": 99}}}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["providers"]["qdrant"]["recall_k"], 99)

    def test_invalid_json_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "broken.json"
            path.write_text("{not valid json")
            cfg = load_config(path)
            self.assertEqual(cfg["version"], DEFAULT_CONFIG["version"])

    def test_project_disabled_marker(self):
        with tempfile.TemporaryDirectory() as td:
            sub = Path(td) / "sub" / "nested"
            sub.mkdir(parents=True)
            self.assertFalse(project_disabled(str(sub), ".claude-hooks-disable"))
            (Path(td) / ".claude-hooks-disable").touch()
            # marker in parent should disable nested cwd
            self.assertTrue(project_disabled(str(sub), ".claude-hooks-disable"))

    def test_expand_user_path(self):
        p = expand_user_path("~/foo")
        self.assertFalse(str(p).startswith("~"))


# ─── v1.10.2 — save_config write-audit log ────────────────────────────
#
# Regression guard for the pandorum 2026-05-22 "config disappeared
# between sessions" incident. ``save_config`` now appends one line per
# successful write to ``~/.claude/claude-hooks-config-writes.log`` so
# next time we can identify the last sanctioned writer.


class TestConfigWriteAuditLog(unittest.TestCase):
    """save_config writes a one-line audit entry to the write log."""

    def test_save_config_appends_audit_line(self):
        """A successful save_config() produces exactly one audit line
        with the expected tab-delimited fields."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "out.json"
            audit_path = tdp / "audit.log"
            with patch.object(config_mod, "CONFIG_WRITE_AUDIT_LOG", audit_path):
                save_config({"version": 2, "x": 1}, cfg_path)
            lines = audit_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1, f"expected 1 line, got: {lines}")
            line = lines[0]
            self.assertIn("\tpid=", line)
            self.assertIn("\tsize=", line)
            self.assertIn(f"\tpath={cfg_path}", line)
            self.assertIn("\tcaller=", line)
            self.assertIn("\targv0=", line)
            # Timestamp is the first field (ISO 8601 with TZ).
            ts = line.split("\t", 1)[0]
            self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_size_field_matches_written_file(self):
        """The ``size=`` field equals the bytes on disk after the
        os.replace — confirms we measure the *final* file, not the
        in-memory dict."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "out.json"
            audit_path = tdp / "audit.log"
            with patch.object(config_mod, "CONFIG_WRITE_AUDIT_LOG", audit_path):
                save_config({"k": "v"}, cfg_path)
            actual_size = cfg_path.stat().st_size
            audit_line = audit_path.read_text(encoding="utf-8").strip()
            self.assertIn(f"\tsize={actual_size}\t", audit_line)

    def test_caller_field_records_test_frame(self):
        """The caller field records this test's filename:line:function —
        confirms inspect.stack() walks past save_config and _audit
        frames to the actual caller."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "out.json"
            audit_path = tdp / "audit.log"
            with patch.object(config_mod, "CONFIG_WRITE_AUDIT_LOG", audit_path):
                save_config({}, cfg_path)  # << this line is the caller frame
            line = audit_path.read_text(encoding="utf-8").strip()
            # We can't pin the exact lineno (tests get edited), but the
            # filename + function name should appear.
            self.assertIn("test_config.py:", line)
            self.assertIn(":test_caller_field_records_test_frame", line)

    def test_append_only_multiple_writes(self):
        """Repeated save_config calls add lines without truncating."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "out.json"
            audit_path = tdp / "audit.log"
            with patch.object(config_mod, "CONFIG_WRITE_AUDIT_LOG", audit_path):
                save_config({"i": 1}, cfg_path)
                save_config({"i": 2}, cfg_path)
                save_config({"i": 3}, cfg_path)
            lines = audit_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)

    def test_audit_failure_does_not_break_save(self):
        """If the audit log can't be written (e.g. permission denied),
        the actual save_config still succeeds. This is the most
        important invariant — audit must never break the real save.

        We simulate the failure by pointing the audit path at a
        directory we explicitly chmod 0 so opening for append raises
        PermissionError. The real cfg_path stays writable."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "out.json"
            # An UNWRITABLE audit target: a directory, not a file.
            # open(directory, "a") raises IsADirectoryError on POSIX
            # and PermissionError on Windows — both swallowed.
            bad_audit = tdp / "audit_is_actually_a_dir"
            bad_audit.mkdir()
            with patch.object(config_mod, "CONFIG_WRITE_AUDIT_LOG", bad_audit):
                # save_config MUST NOT raise.
                returned = save_config({"alive": True}, cfg_path)
            self.assertEqual(returned, cfg_path)
            self.assertTrue(cfg_path.is_file())
            saved = json.loads(cfg_path.read_text())
            self.assertEqual(saved, {"alive": True})

    def test_audit_never_logs_config_content(self):
        """Sensitive content (e.g. pgvector DSN with password) must NOT
        appear in the audit line. Critical security invariant — the
        audit log lives under ~/.claude/ which is less protected than
        config/. Metadata only."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "out.json"
            audit_path = tdp / "audit.log"
            sensitive_dsn = (
                "postgresql://claude:Xct29ERYVvn6iTme6dt544pd3sP6o1Qm@"
                "192.168.178.2:5432/memory"
            )
            cfg = {
                "providers": {
                    "pgvector": {"dsn": sensitive_dsn, "api_key": "sk-secret"},
                },
            }
            with patch.object(config_mod, "CONFIG_WRITE_AUDIT_LOG", audit_path):
                save_config(cfg, cfg_path)
            audit_text = audit_path.read_text(encoding="utf-8")
            # Neither the password nor the api_key substring may
            # appear in the audit log.
            self.assertNotIn("Xct29ERYVvn6iTme6dt544pd3sP6o1Qm", audit_text)
            self.assertNotIn("sk-secret", audit_text)
            self.assertNotIn(sensitive_dsn, audit_text)

    def test_audit_log_default_path_under_claude_dir(self):
        """The module-level ``CONFIG_WRITE_AUDIT_LOG`` resolves to
        ``~/.claude/claude-hooks-config-writes.log``, matching the
        sibling log paths (claude-hooks.log, lsp-engine.log)."""
        p = config_mod.CONFIG_WRITE_AUDIT_LOG
        # Must be under the user's .claude dir, must end with the
        # documented filename.
        self.assertEqual(p.name, "claude-hooks-config-writes.log")
        self.assertEqual(p.parent.name, ".claude")

    def test_audit_helper_directly_with_synthetic_caller(self):
        """Calling _audit_log_config_write directly produces a valid
        line — useful for callers that might want to log auxiliary
        config-equivalent writes in the future without going through
        save_config (none today, but the helper is part of the
        public-ish surface)."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "anywhere.json"
            audit_path = tdp / "audit.log"
            with patch.object(config_mod, "CONFIG_WRITE_AUDIT_LOG", audit_path):
                _audit_log_config_write(cfg_path, 1234)
            line = audit_path.read_text(encoding="utf-8").strip()
            self.assertIn("\tsize=1234\t", line)
            self.assertIn(f"\tpath={cfg_path}\t", line)


if __name__ == "__main__":
    unittest.main()
