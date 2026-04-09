"""Tests for config load/save and project disable marker."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks.config import (
    DEFAULT_CONFIG,
    expand_user_path,
    load_config,
    project_disabled,
    save_config,
)


class TestConfig(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
