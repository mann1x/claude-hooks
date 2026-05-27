"""Tests for the memory/KG MCP allow-rule injection (v1.11.2+).

In `auto` permission-mode, Claude Code routes any tool call not matched
by a static `permissions.allow` rule through a safety classifier (an LLM
call). Memory/KG MCP tools are writes, so the classifier gates them --
and blocks them outright when the upstream the classifier rides is
flapping. ``install.py`` therefore allow-lists the memory backends so
recall/store auto-approve without the classifier.

Covers:
- ``MEMORY_ALLOW_RULES`` covers all five backend keys (pgvector,
  sqlite_vec, qdrant, memory_kg, memory) in wildcard form.
- ``_ensure_memory_allow_rules`` adds all rules to a fresh/absent file.
- Idempotent: a second run adds nothing.
- Existing ``permissions.allow`` entries are preserved, no duplicates.
- ``dry_run=True`` previews without creating/modifying the file.
- A backup is written when the target already exists.
- ``uninstall`` removes our exact wildcard rules but leaves foreign ones.
- The ``--sync-permissions`` CLI entrypoint applies the rules and exits 0.
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


class MemoryAllowRuleConstantsTests(unittest.TestCase):
    def test_covers_all_five_backends(self):
        for key in ("pgvector", "sqlite_vec", "qdrant", "memory_kg", "memory"):
            self.assertIn(f"mcp__{key}__*", install.MEMORY_ALLOW_RULES)

    def test_rules_are_wildcard_form(self):
        for rule in install.MEMORY_ALLOW_RULES:
            self.assertTrue(rule.startswith("mcp__"))
            self.assertTrue(rule.endswith("__*"))


class EnsureMemoryAllowRulesTests(unittest.TestCase):
    def test_adds_all_rules_to_absent_file(self):
        with TemporaryDirectory() as d:
            sp = Path(d) / "settings.json"
            added = install._ensure_memory_allow_rules(sp, _print=False)
            self.assertEqual(sorted(added), sorted(install.MEMORY_ALLOW_RULES))
            data = json.loads(sp.read_text())
            self.assertEqual(
                data["permissions"]["allow"], list(install.MEMORY_ALLOW_RULES)
            )

    def test_idempotent_second_run_adds_nothing(self):
        with TemporaryDirectory() as d:
            sp = Path(d) / "settings.json"
            install._ensure_memory_allow_rules(sp, _print=False)
            again = install._ensure_memory_allow_rules(sp, _print=False)
            self.assertEqual(again, [])
            data = json.loads(sp.read_text())
            # No duplicates introduced by the re-run.
            allow = data["permissions"]["allow"]
            for rule in install.MEMORY_ALLOW_RULES:
                self.assertEqual(allow.count(rule), 1)

    def test_preserves_existing_entries_no_dupes(self):
        with TemporaryDirectory() as d:
            sp = Path(d) / "settings.json"
            sp.write_text(json.dumps({
                "permissions": {"allow": ["Bash(git *)", "mcp__pgvector__*"]}
            }))
            added = install._ensure_memory_allow_rules(sp, _print=False)
            # pgvector already present -> not re-added.
            self.assertNotIn("mcp__pgvector__*", added)
            self.assertIn("mcp__memory__*", added)
            allow = json.loads(sp.read_text())["permissions"]["allow"]
            self.assertIn("Bash(git *)", allow)          # foreign entry kept
            self.assertEqual(allow.count("mcp__pgvector__*"), 1)  # no dup

    def test_dry_run_writes_nothing(self):
        with TemporaryDirectory() as d:
            sp = Path(d) / "settings.json"
            preview = install._ensure_memory_allow_rules(
                sp, dry_run=True, _print=False
            )
            self.assertEqual(preview, list(install.MEMORY_ALLOW_RULES))
            self.assertFalse(sp.exists())

    def test_backup_written_when_file_exists(self):
        with TemporaryDirectory() as d:
            sp = Path(d) / "settings.json"
            sp.write_text(json.dumps({"permissions": {"allow": []}}))
            install._ensure_memory_allow_rules(sp, _print=False)
            baks = list(Path(d).glob("settings.json.bak-*-memory-allowlist"))
            self.assertEqual(len(baks), 1, baks)

    def test_tolerates_non_dict_permissions(self):
        with TemporaryDirectory() as d:
            sp = Path(d) / "settings.json"
            sp.write_text(json.dumps({"permissions": "garbage"}))
            added = install._ensure_memory_allow_rules(sp, _print=False)
            self.assertEqual(sorted(added), sorted(install.MEMORY_ALLOW_RULES))
            data = json.loads(sp.read_text())
            self.assertEqual(
                data["permissions"]["allow"], list(install.MEMORY_ALLOW_RULES)
            )


class UninstallRemovesMemoryRulesTests(unittest.TestCase):
    def test_uninstall_drops_our_rules_keeps_foreign(self):
        with TemporaryDirectory() as d:
            sp = Path(d) / "settings.json"
            sp.write_text(json.dumps({
                "permissions": {
                    "allow": ["Bash(git *)", "mcp__memory__search_nodes"]
                                + list(install.MEMORY_ALLOW_RULES)
                },
                "hooks": {},
            }))
            with patch.object(install, "user_settings_path", return_value=sp), \
                 patch.object(install, "_remove_bin_shim_wrappers", return_value=0):
                rc = install.uninstall(dry_run=False)
            self.assertEqual(rc, 0)
            allow = json.loads(sp.read_text())["permissions"]["allow"]
            for rule in install.MEMORY_ALLOW_RULES:
                self.assertNotIn(rule, allow)
            # Foreign + hand-added per-tool memory grant untouched.
            self.assertIn("Bash(git *)", allow)
            self.assertIn("mcp__memory__search_nodes", allow)


class SyncPermissionsCliTests(unittest.TestCase):
    def test_sync_permissions_flag_applies_and_exits_zero(self):
        with TemporaryDirectory() as d:
            sp = Path(d) / "settings.json"
            with patch.object(install, "user_settings_path", return_value=sp), \
                 patch.object(sys, "argv", ["install.py", "--sync-permissions"]):
                rc = install.main()
            self.assertEqual(rc, 0)
            allow = json.loads(sp.read_text())["permissions"]["allow"]
            for rule in install.MEMORY_ALLOW_RULES:
                self.assertIn(rule, allow)

    def test_sync_permissions_dry_run_writes_nothing(self):
        with TemporaryDirectory() as d:
            sp = Path(d) / "settings.json"
            with patch.object(install, "user_settings_path", return_value=sp), \
                 patch.object(sys, "argv",
                              ["install.py", "--sync-permissions", "--dry-run"]):
                rc = install.main()
            self.assertEqual(rc, 0)
            self.assertFalse(sp.exists())


if __name__ == "__main__":
    unittest.main()
