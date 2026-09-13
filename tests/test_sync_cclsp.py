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


class NodeShimTests(unittest.TestCase):
    """Node refuses to spawn .cmd/.bat with shell:false (CVE-2024-27980).

    Every npm-installed language server on Windows is a .cmd shim, so
    all of them raised EINVAL under cclsp. Measured on pandorum: 6 of 12
    servers dead, including pyright, which predated this script.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _shim(self, name: str, target_rel: str) -> Path:
        # target_rel is spelled with backslashes because that is what the
        # shim contains. Build the real file from its parts, or on POSIX
        # this creates one file whose *name* contains backslashes.
        target = self.dir.joinpath(*target_rel.split("\\"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// entry", encoding="utf-8")
        shim = self.dir / name
        shim.write_text(
            '@ECHO off\r\n'
            'IF EXIST "%dp0%\\node.exe" (\r\n'
            '  SET "_prog=%dp0%\\node.exe"\r\n'
            ') ELSE (\r\n  SET "_prog=node"\r\n)\r\n'
            f'endLocal & "%_prog%"  "%dp0%\\{target_rel}" %*\r\n',
            encoding="utf-8")
        return shim

    def test_cmd_shim_is_rewritten_to_node_plus_script(self):
        shim = self._shim("pyright-langserver.cmd",
                          r"node_modules\pyright\langserver.index.js")
        cmd, rewritten = sync_cclsp.spawnable_command(str(shim), ["--stdio"])
        self.assertTrue(rewritten)
        self.assertEqual(cmd[0], "node")
        self.assertTrue(cmd[1].endswith("langserver.index.js"))
        self.assertEqual(cmd[2], "--stdio")

    def test_interpreter_reference_is_not_mistaken_for_the_target(self):
        """The shim names node.exe *before* its target; naive
        first-match resolution returned node.exe itself."""
        shim = self._shim("ts.cmd", r"node_modules\x\lib\cli.mjs")
        cmd, _ = sync_cclsp.spawnable_command(str(shim), [])
        self.assertTrue(cmd[1].endswith("cli.mjs"), cmd)

    def test_extensionless_target_resolves(self):
        """vscode-langservers-extracted wraps a file with no extension,
        and typescript-language-server a .mjs — a `.js`-only pattern
        silently matched neither."""
        shim = self._shim("vscode-html-language-server.CMD",
                          r"node_modules\v\bin\vscode-html-language-server")
        cmd, rewritten = sync_cclsp.spawnable_command(str(shim), ["--stdio"])
        self.assertTrue(rewritten)
        self.assertTrue(cmd[1].endswith("vscode-html-language-server"))

    def test_exe_is_left_alone(self):
        cmd, rewritten = sync_cclsp.spawnable_command(
            r"C:\tools\clangd.exe", [])
        self.assertFalse(rewritten)
        self.assertEqual(cmd, [r"C:\tools\clangd.exe"])

    def test_posix_path_is_left_alone(self):
        cmd, rewritten = sync_cclsp.spawnable_command(
            "/usr/local/bin/pyright-langserver", ["--stdio"])
        self.assertFalse(rewritten)
        self.assertEqual(cmd, ["/usr/local/bin/pyright-langserver", "--stdio"])

    def test_unresolvable_shim_is_reported_not_silently_kept(self):
        missing = self.dir / "ghost.cmd"
        missing.write_text("@ECHO off\r\n", encoding="utf-8")
        cmd, rewritten = sync_cclsp.spawnable_command(str(missing), [])
        self.assertFalse(rewritten)


class DedupeTests(unittest.TestCase):
    """A rewritten command keys as ``node``, so a second sync run used
    to re-add every repaired server. pandorum's config grew 12 -> 14."""

    def test_repaired_entry_is_found_again_by_extension(self):
        cfg = {"servers": [
            {"extensions": ["ts", "js"],
             "command": ["node", r"C:\npm\node_modules\x\cli.mjs"]},
        ]}

        class S:
            bin = "typescript-language-server"
            extensions = ("ts", "js")
            cclsp_command = ("typescript-language-server", "--stdio")

        self.assertIsNotNone(sync_cclsp._find_entry(cfg, S()))

    def test_second_run_does_not_duplicate(self):
        class S:
            bin = "typescript-language-server"
            extensions = ("ts", "js")
            cclsp_command = ("typescript-language-server", "--stdio")

        cfg = {"servers": []}
        with mock.patch.object(sync_cclsp, "SPECS", [S()]), \
             mock.patch.object(sync_cclsp.shutil, "which",
                               return_value="/usr/bin/tsls"):
            cfg, _ = sync_cclsp.reconcile(cfg, resolve_commands=True)
            cfg["servers"][0]["command"] = ["node", "/x/cli.mjs"]  # as repaired
            cfg, _ = sync_cclsp.reconcile(cfg, resolve_commands=True)
        self.assertEqual(len(cfg["servers"]), 1, cfg["servers"])

    def test_dedupe_drops_the_fully_covered_duplicate(self):
        cfg = {"servers": [
            {"extensions": ["html", "htm"], "command": ["a"]},
            {"extensions": ["html", "htm"], "command": ["node", "b"]},
        ]}
        notes = sync_cclsp.dedupe_servers(cfg)
        self.assertEqual(len(cfg["servers"]), 1)
        self.assertTrue(any("DEDUPE" in n for n in notes))

    def test_dedupe_keeps_distinct_servers(self):
        cfg = {"servers": [
            {"extensions": ["py"], "command": ["a"]},
            {"extensions": ["go"], "command": ["b"]},
        ]}
        sync_cclsp.dedupe_servers(cfg)
        self.assertEqual(len(cfg["servers"]), 2)

    def test_dedupe_keeps_an_entry_that_adds_new_extensions(self):
        """Partial overlap is not duplication — dropping it would lose
        the extensions only that entry claims."""
        cfg = {"servers": [
            {"extensions": ["ts"], "command": ["a"]},
            {"extensions": ["ts", "tsx"], "command": ["b"]},
        ]}
        sync_cclsp.dedupe_servers(cfg)
        self.assertEqual(len(cfg["servers"]), 2)


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
