"""Tests for ``claude_hooks.models_cli`` — the
``claude-hooks-models`` registry CLI.

Each subcommand's happy + error paths, with the registry file under
``tmp_path`` and a fake GGUF that passes the magic check.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import io
import sys

from claude_hooks import models_cli
from claude_hooks.chat_model_registry import Registry


def _make_fake_gguf(tmp_dir: Path, name: str = "model.gguf") -> Path:
    p = tmp_dir / name
    # GGUF magic (4 bytes) + a few padding bytes so it's a real file
    p.write_bytes(b"GGUF" + b"\x00" * 12)
    return p


def _run(argv: list[str], registry_path: Path) -> tuple[int, str, str]:
    """Invoke models_cli.main, return (rc, stdout, stderr)."""
    full = ["--registry", str(registry_path)] + argv
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    rc = 0
    with patch.object(sys, "stdout", out_buf), \
         patch.object(sys, "stderr", err_buf):
        try:
            rc = models_cli.main(full)
        except SystemExit as e:
            rc = int(e.code or 0)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class TestModelsCli(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.registry_path = self.tmp_path / "registry.json"
        self.gguf = _make_fake_gguf(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    # --- path / list (empty) ---

    def test_path_prints_registry_path(self):
        rc, out, _ = _run(["path"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), str(self.registry_path))

    def test_list_empty(self):
        rc, out, _ = _run(["list"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertIn("no models registered", out)

    def test_list_json_empty(self):
        rc, out, _ = _run(["list", "--json"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [])

    # --- add ---

    def test_add_happy(self):
        rc, out, _ = _run(
            ["add", "gemma", str(self.gguf), "--ctx", "8192"],
            self.registry_path,
        )
        self.assertEqual(rc, 0)
        self.assertIn("added 'gemma'", out)
        reg = Registry(self.registry_path)
        self.assertEqual(reg.get("gemma").ctx_size, 8192)

    def test_add_label_collision(self):
        _run(["add", "g", str(self.gguf)], self.registry_path)
        rc, _, err = _run(
            ["add", "g", str(self.gguf)], self.registry_path,
        )
        self.assertEqual(rc, 2)
        self.assertIn("label exists", err)

    def test_add_invalid_label(self):
        rc, _, err = _run(
            ["add", "BADLABEL", str(self.gguf)], self.registry_path,
        )
        self.assertEqual(rc, 2)
        self.assertIn("invalid label", err)

    def test_add_missing_gguf(self):
        rc, _, err = _run(
            ["add", "g", str(self.tmp_path / "no-such-file.gguf")],
            self.registry_path,
        )
        self.assertEqual(rc, 2)
        self.assertIn("invalid gguf", err)

    def test_add_bad_gguf_magic(self):
        bad = self.tmp_path / "bad.gguf"
        bad.write_bytes(b"NOPE" + b"\x00" * 8)
        rc, _, err = _run(
            ["add", "g", str(bad)], self.registry_path,
        )
        self.assertEqual(rc, 2)
        self.assertIn("invalid gguf", err)

    def test_add_port_collision(self):
        _run(["add", "a", str(self.gguf), "--port", "38093"],
             self.registry_path)
        rc, _, err = _run(
            ["add", "b", str(self.gguf), "--port", "38093"],
            self.registry_path,
        )
        self.assertEqual(rc, 2)
        self.assertIn("port conflict", err)

    # --- list (populated) + show ---

    def test_list_populated(self):
        _run(["add", "g", str(self.gguf)], self.registry_path)
        rc, out, _ = _run(["list"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertIn("g", out)
        self.assertIn("port", out)
        self.assertIn(str(self.gguf), out)

    def test_show_happy(self):
        _run(["add", "g", str(self.gguf), "--notes", "hi"],
             self.registry_path)
        # Patch daemon_client lookup to silence the best-effort RPC.
        with patch.object(models_cli, "_best_effort_status",
                          return_value=None):
            rc, out, _ = _run(["show", "g"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertIn("label: g", out)
        self.assertIn("notes: hi", out)

    def test_show_unknown(self):
        rc, _, err = _run(["show", "nope"], self.registry_path)
        self.assertEqual(rc, 2)
        self.assertIn("unknown label", err)

    def test_show_json(self):
        _run(["add", "g", str(self.gguf)], self.registry_path)
        with patch.object(models_cli, "_best_effort_status",
                          return_value=None):
            rc, out, _ = _run(["show", "g", "--json"], self.registry_path)
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["label"], "g")

    # --- remove ---

    def test_remove_happy(self):
        _run(["add", "g", str(self.gguf)], self.registry_path)
        # The remove command does a best-effort daemon shutdown; stub it.
        with patch.object(models_cli, "_best_effort_shutdown") as bes:
            rc, out, _ = _run(["remove", "g"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertIn("removed 'g'", out)
        bes.assert_called_once_with("g")

    def test_remove_force_skips_daemon_call(self):
        _run(["add", "g", str(self.gguf)], self.registry_path)
        with patch.object(models_cli, "_best_effort_shutdown") as bes:
            rc, _, _ = _run(
                ["remove", "g", "--force"], self.registry_path,
            )
        self.assertEqual(rc, 0)
        bes.assert_not_called()

    def test_remove_unknown(self):
        rc, _, err = _run(["remove", "nope"], self.registry_path)
        self.assertEqual(rc, 2)
        self.assertIn("unknown label", err)

    # --- rename ---

    def test_rename_happy(self):
        _run(["add", "g", str(self.gguf)], self.registry_path)
        rc, out, _ = _run(["rename", "g", "h"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertIn("renamed 'g' -> 'h'", out)
        reg = Registry(self.registry_path)
        self.assertFalse(reg.exists("g"))
        self.assertTrue(reg.exists("h"))

    def test_rename_unknown(self):
        rc, _, err = _run(["rename", "no", "yes"], self.registry_path)
        self.assertEqual(rc, 2)
        self.assertIn("unknown label", err)

    def test_rename_collision(self):
        _run(["add", "a", str(self.gguf)], self.registry_path)
        _run(["add", "b", str(self.gguf)], self.registry_path)
        rc, _, err = _run(["rename", "a", "b"], self.registry_path)
        self.assertEqual(rc, 2)
        self.assertIn("label exists", err)

    # --- copy ---

    def test_copy_happy(self):
        _run(["add", "src", str(self.gguf)], self.registry_path)
        rc, out, _ = _run(["copy", "src", "dst"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertIn("copied 'src' -> 'dst'", out)
        reg = Registry(self.registry_path)
        self.assertEqual(reg.get("src").gguf_path,
                         reg.get("dst").gguf_path)
        self.assertNotEqual(reg.get("src").port, reg.get("dst").port)

    def test_copy_override_ctx(self):
        _run(["add", "src", str(self.gguf), "--ctx", "8192"],
             self.registry_path)
        rc, _, _ = _run(
            ["copy", "src", "big", "--ctx", "32768"], self.registry_path,
        )
        self.assertEqual(rc, 0)
        reg = Registry(self.registry_path)
        self.assertEqual(reg.get("big").ctx_size, 32768)

    def test_copy_unknown_source(self):
        rc, _, err = _run(["copy", "nope", "x"], self.registry_path)
        self.assertEqual(rc, 2)
        self.assertIn("unknown source", err)

    # --- probe (daemon down) ---

    def test_probe_daemon_down(self):
        _run(["add", "g", str(self.gguf)], self.registry_path)
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value=None):
            rc, _, err = _run(["probe", "g"], self.registry_path)
        self.assertEqual(rc, 2)
        self.assertIn("daemon not reachable", err)

    def test_probe_unknown_label(self):
        rc, _, err = _run(["probe", "nope"], self.registry_path)
        self.assertEqual(rc, 2)
        self.assertIn("unknown label", err)

    def test_probe_happy(self):
        _run(["add", "g", str(self.gguf)], self.registry_path)
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38093,
                                 "mode": "auto", "spawned": True,
                                 "evicted": []}):
            rc, out, _ = _run(["probe", "g"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertIn("port 38093", out)

    def test_probe_with_evicted(self):
        _run(["add", "g", str(self.gguf)], self.registry_path)
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38093,
                                 "spawned": True, "evicted": ["older"]}):
            rc, out, _ = _run(["probe", "g"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertIn("evicted to make room", out)
        self.assertIn("older", out)

    # --- gc ---

    def test_gc_daemon_down(self):
        with patch("claude_hooks.daemon_client.chat_model_gc",
                   return_value=None):
            rc, _, err = _run(["gc"], self.registry_path)
        self.assertEqual(rc, 2)
        self.assertIn("daemon not reachable", err)

    def test_gc_happy_with_orphans(self):
        with patch("claude_hooks.daemon_client.chat_model_gc",
                   return_value={"ok": True, "reaped": ["a", "b"]}):
            rc, out, _ = _run(["gc"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertIn("reaped 2", out)

    def test_gc_no_orphans(self):
        with patch("claude_hooks.daemon_client.chat_model_gc",
                   return_value={"ok": True, "reaped": []}):
            rc, out, _ = _run(["gc"], self.registry_path)
        self.assertEqual(rc, 0)
        self.assertIn("no orphans", out)


if __name__ == "__main__":
    unittest.main()
