"""Tests for ``scripts/sync_cclsp.py``.

The bug this file exists to prevent: there are **two** consumers of a
``cclsp.json`` and they read different files. The built-in engine reads
``<project>/cclsp.json``; the ``lsp`` MCP server (the third-party
``cclsp`` binary, shared with VS Code) reads ``$CCLSP_CONFIG_PATH``.

On 2026-09-13 both hosts ran a 9-server engine config beside a 5-server
MCP config. Asking the MCP about a ``.js`` file returned "No LSP servers
found" while the engine answered correctly — and the pass that was meant
to close that gap had verified the engine only, then declared the
subsystem healthy.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "sync_cclsp", REPO / "scripts" / "sync_cclsp.py")
sync_cclsp = importlib.util.module_from_spec(_spec)
sys.modules["sync_cclsp"] = sync_cclsp
_spec.loader.exec_module(sync_cclsp)


class BinKeyTests(unittest.TestCase):
    """``_bin_key`` is what stops a duplicate entry being proposed."""

    def test_absolute_posix_path_matches_bare_name(self):
        self.assertEqual(sync_cclsp._bin_key("/root/go/bin/gopls"),
                         sync_cclsp._bin_key("gopls"))

    def test_windows_exe_suffix_matches_bare_name(self):
        self.assertEqual(sync_cclsp._bin_key("gopls.exe"),
                         sync_cclsp._bin_key("gopls"))

    def test_windows_cmd_shim_matches_bare_name(self):
        self.assertEqual(
            sync_cclsp._bin_key(
                r"C:\Users\m\AppData\Roaming\npm\typescript-language-server.CMD"),
            sync_cclsp._bin_key("typescript-language-server"),
        )

    def test_case_is_normalised(self):
        self.assertEqual(sync_cclsp._bin_key("OmniSharp.exe"),
                         sync_cclsp._bin_key("omnisharp"))

    def test_distinct_servers_stay_distinct(self):
        self.assertNotEqual(sync_cclsp._bin_key("gopls"),
                            sync_cclsp._bin_key("clangd"))


class ReconcileTests(unittest.TestCase):

    def _spec_stub(self, name="gopls", ext=("go",)):
        class S:
            bin = name
            extensions = ext
            cclsp_command = (name,)
        return S()

    def test_already_mapped_server_is_not_duplicated(self):
        """The exact regression: an absolute path is still a mapping."""
        cfg = {"servers": [
            {"extensions": ["go"], "command": ["/root/go/bin/gopls"]},
        ]}
        with mock.patch.object(sync_cclsp, "SPECS", [self._spec_stub()]), \
             mock.patch.object(sync_cclsp.shutil, "which",
                               return_value="/root/go/bin/gopls"):
            out, notes = sync_cclsp.reconcile(cfg)
        self.assertEqual(len(out["servers"]), 1, notes)
        self.assertEqual([n for n in notes if "ADD" in n], [])

    def test_missing_server_is_added(self):
        cfg = {"servers": []}
        with mock.patch.object(sync_cclsp, "SPECS", [self._spec_stub()]), \
             mock.patch.object(sync_cclsp.shutil, "which",
                               return_value="/usr/bin/gopls"):
            out, notes = sync_cclsp.reconcile(cfg)
        self.assertEqual(len(out["servers"]), 1)
        self.assertTrue(any("ADD" in n for n in notes))

    def test_uninstalled_server_is_never_added(self):
        cfg = {"servers": []}
        with mock.patch.object(sync_cclsp, "SPECS", [self._spec_stub()]), \
             mock.patch.object(sync_cclsp.shutil, "which", return_value=None):
            out, _ = sync_cclsp.reconcile(cfg)
        self.assertEqual(out["servers"], [])

    def test_resolve_commands_writes_the_resolved_path(self):
        """Windows: cclsp spawns the configured name, and CreateProcess
        does not consult PATHEXT. A bare name is invisible there."""
        cfg = {"servers": []}
        shim = r"C:\npm\typescript-language-server.CMD"
        spec = self._spec_stub("typescript-language-server", ("ts", "js"))
        with mock.patch.object(sync_cclsp, "SPECS", [spec]), \
             mock.patch.object(sync_cclsp.shutil, "which", return_value=shim):
            out, _ = sync_cclsp.reconcile(cfg, resolve_commands=True)
        self.assertEqual(out["servers"][0]["command"][0], shim)

    def test_without_resolve_commands_the_bare_name_is_kept(self):
        cfg = {"servers": []}
        spec = self._spec_stub("typescript-language-server", ("ts",))
        with mock.patch.object(sync_cclsp, "SPECS", [spec]), \
             mock.patch.object(sync_cclsp.shutil, "which",
                               return_value="/usr/bin/typescript-language-server"):
            out, _ = sync_cclsp.reconcile(cfg, resolve_commands=False)
        self.assertEqual(out["servers"][0]["command"][0],
                         "typescript-language-server")

    def test_existing_command_is_never_overwritten(self):
        """A hand-tuned command survives; only extensions are extended."""
        cfg = {"servers": [
            {"extensions": ["ts"], "command": ["/opt/custom/tsls", "--stdio"]},
        ]}
        spec = self._spec_stub("tsls", ("ts", "js"))
        with mock.patch.object(sync_cclsp, "SPECS", [spec]), \
             mock.patch.object(sync_cclsp.shutil, "which",
                               return_value="/usr/bin/tsls"):
            out, _ = sync_cclsp.reconcile(cfg, resolve_commands=True)
        self.assertEqual(out["servers"][0]["command"],
                         ["/opt/custom/tsls", "--stdio"])
        self.assertIn("js", out["servers"][0]["extensions"])


class McpConfigPathTests(unittest.TestCase):
    """Uses a real isolated home rather than patching ``pathlib``.

    An earlier version patched ``Path.read_text`` at class level, which
    passed on Linux and raised from inside pathlib on Windows — where
    ``Path`` instantiation goes through machinery that a blanket class
    patch disturbs. Both HOME *and* USERPROFILE are set: ``Path.home()``
    reads USERPROFILE on Windows, so a HOME-only fixture silently
    no-ops there and the test reads the developer's real config.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _isolated(self, extra_env=None):
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        env.update(extra_env or {})
        return mock.patch.dict(os.environ, env, clear=True)

    def _write_claude_json(self, data):
        (self.home / ".claude.json").write_text(
            json.dumps(data), encoding="utf-8")

    def test_env_var_wins(self):
        target = str(self.home / "explicit" / "cclsp.json")
        with self._isolated({"CCLSP_CONFIG_PATH": target}):
            self.assertEqual(sync_cclsp.mcp_config_path(), Path(target))

    def test_reads_the_path_claude_json_declares(self):
        """The MCP is launched with that env var set, so the file it
        actually reads is recorded there and nowhere else."""
        declared = str(self.home / ".config" / "cclsp" / "cclsp.json")
        self._write_claude_json({"mcpServers": {"lsp": {
            "command": "cclsp.cmd",
            "env": {"CCLSP_CONFIG_PATH": declared},
        }}})
        with self._isolated():
            self.assertEqual(sync_cclsp.mcp_config_path(), Path(declared))

    def test_falls_back_when_there_is_no_claude_json(self):
        with self._isolated():
            self.assertEqual(sync_cclsp.mcp_config_path(),
                             sync_cclsp.DEFAULT_MCP_CONFIG)

    def test_falls_back_when_claude_json_is_corrupt(self):
        (self.home / ".claude.json").write_text("{not json", encoding="utf-8")
        with self._isolated():
            self.assertEqual(sync_cclsp.mcp_config_path(),
                             sync_cclsp.DEFAULT_MCP_CONFIG)

    def test_non_cclsp_mcp_entries_are_ignored(self):
        self._write_claude_json({"mcpServers": {
            "pgvector": {"command": "claude-hook-pgvector-mcp",
                         "env": {"CCLSP_CONFIG_PATH": "/wrong/one.json"}},
        }})
        with self._isolated():
            self.assertEqual(sync_cclsp.mcp_config_path(),
                             sync_cclsp.DEFAULT_MCP_CONFIG)


class SpecCoverageTests(unittest.TestCase):
    """Every extension a spec claims must have a real languageId.

    An extension mapped in cclsp.json but announced as ``plaintext`` is
    worse than an unmapped one: the server accepts the document and
    silently declines to analyse it.
    """

    def test_every_spec_extension_has_a_language_id(self):
        from claude_hooks.lang_servers import SPECS
        from claude_hooks.lsp_engine.lsp import language_id_for
        missing = [
            (spec.bin, ext)
            for spec in SPECS
            for ext in spec.extensions
            if language_id_for(f"x.{ext}") == "plaintext"
        ]
        self.assertEqual(missing, [], f"unmapped languageIds: {missing}")

    def test_html_is_not_routed_to_a_javascript_server(self):
        """tsserver silently declines a document it doesn't claim, so
        html -> typescript would buy an accepted file and an empty
        diagnostic list."""
        from claude_hooks.lang_servers import SPECS
        for spec in SPECS:
            if "html" in spec.extensions:
                self.assertIn("html", spec.bin)


if __name__ == "__main__":
    unittest.main()
