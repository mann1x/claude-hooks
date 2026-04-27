"""Tests for OpenWolf integration — extracting .wolf/ data for recall/store."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks.openwolf import recall_context, wolf_dir


class TestOpenWolf(unittest.TestCase):
    def _make_wolf(self, td: str) -> Path:
        """Create a minimal .wolf/ directory with test data."""
        wd = Path(td) / ".wolf"
        wd.mkdir()

        # cerebrum.md
        (wd / "cerebrum.md").write_text(
            "# Cerebrum\n\n"
            "## User Preferences\n\n"
            "- Use tabs not spaces\n\n"
            "## Key Learnings\n\n"
            "- **Project:** test-proj\n"
            "- Tests go in tests/ not __tests__/\n\n"
            "## Do-Not-Repeat\n\n"
            "- [2026-04-01] Never use rm -rf / without checking\n"
            "- [2026-04-02] Always validate SQL table names\n\n"
            "## Decision Log\n\n"
            "- [2026-04-01] Chose SQLite over Postgres for simplicity\n"
        )

        # buglog.json
        (wd / "buglog.json").write_text(json.dumps({
            "version": 1,
            "bugs": [
                {
                    "id": "bug-001",
                    "timestamp": "2026-04-01T10:00:00Z",
                    "error_message": "KeyError on missing config",
                    "file": "config.py",
                    "root_cause": "no default value",
                    "fix": "added .get() with default",
                    "tags": ["auto-detected"],
                    "occurrences": 1,
                    "last_seen": "2026-04-01T10:00:00Z",
                },
            ],
        }))
        return wd

    def test_wolf_dir_found(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_wolf(td)
            self.assertIsNotNone(wolf_dir(td))

    def test_wolf_dir_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(wolf_dir(td))

    def test_wolf_dir_empty_cwd(self):
        self.assertIsNone(wolf_dir(""))

    def test_recall_context_includes_dnr(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_wolf(td)
            ctx = recall_context(td)
            self.assertIsNotNone(ctx)
            self.assertIn("Do-Not-Repeat", ctx)
            self.assertIn("rm -rf", ctx)
            self.assertIn("SQL table names", ctx)

    def test_recall_context_includes_bugs(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_wolf(td)
            ctx = recall_context(td)
            self.assertIn("bug-001", ctx)
            self.assertIn("KeyError", ctx)

    def test_recall_context_no_wolf(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(recall_context(td))

    def test_empty_cerebrum_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td) / ".wolf"
            wd.mkdir()
            (wd / "cerebrum.md").write_text(
                "# Cerebrum\n\n## Do-Not-Repeat\n\n<!-- empty -->\n\n## Key Learnings\n\n"
            )
            (wd / "buglog.json").write_text('{"version":1,"bugs":[]}')
            self.assertIsNone(recall_context(td))


if __name__ == "__main__":
    unittest.main()
