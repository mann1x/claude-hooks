"""Tests for ``claude_hooks.lsp_engine.config``.

Two layers of config:
- ``cclsp.json`` (canonical LSP wiring, read-only)
- ``.claude-hooks/lsp-engine.json`` + global ``hooks.lsp_engine`` block
  (engine knobs, layered)

These tests cover both. No subprocess, no LSP — pure config parsing.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_hooks.lsp_engine.config import (
    CclspConfigError,
    EngineConfig,
    LspServerSpec,
    load_cclsp_config,
    load_engine_config,
    resolve_server_for_path,
)


class TestLoadCclspConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cclsp.json"

    def _write(self, payload) -> None:
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_file_returns_empty_list(self) -> None:
        self.assertEqual(load_cclsp_config("/no/such/file.json"), [])

    def test_parses_full_solidpc_shape(self) -> None:
        self._write(
            {
                "servers": [
                    {
                        "extensions": ["py", "pyi"],
                        "command": ["pyright-langserver", "--stdio"],
                        "rootDir": ".",
                    },
                    {
                        "extensions": ["go"],
                        "command": ["/root/go/bin/gopls"],
                    },
                ]
            }
        )
        servers = load_cclsp_config(self.path)
        self.assertEqual(len(servers), 2)
        self.assertEqual(servers[0].extensions, ("py", "pyi"))
        self.assertEqual(servers[0].command, ("pyright-langserver", "--stdio"))
        self.assertEqual(servers[0].root_dir, ".")
        self.assertEqual(servers[1].extensions, ("go",))
        # rootDir defaults to "." when omitted.
        self.assertEqual(servers[1].root_dir, ".")

    def test_lowercases_and_strips_dots_from_extensions(self) -> None:
        self._write(
            {
                "servers": [
                    {
                        "extensions": [".PY", "Pyi", ".GO"],
                        "command": ["x"],
                    }
                ]
            }
        )
        servers = load_cclsp_config(self.path)
        self.assertEqual(servers[0].extensions, ("py", "pyi", "go"))

    def test_invalid_json_raises(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(CclspConfigError):
            load_cclsp_config(self.path)

    def test_servers_must_be_a_list(self) -> None:
        self._write({"servers": "nope"})
        with self.assertRaises(CclspConfigError):
            load_cclsp_config(self.path)

    def test_server_entry_must_be_object(self) -> None:
        self._write({"servers": ["not a dict"]})
        with self.assertRaises(CclspConfigError):
            load_cclsp_config(self.path)

    def test_server_extensions_required(self) -> None:
        self._write({"servers": [{"command": ["x"]}]})
        with self.assertRaises(CclspConfigError):
            load_cclsp_config(self.path)

    def test_server_command_required(self) -> None:
        self._write({"servers": [{"extensions": ["py"]}]})
        with self.assertRaises(CclspConfigError):
            load_cclsp_config(self.path)


class TestResolveServerForPath(unittest.TestCase):
    def setUp(self) -> None:
        self.py = LspServerSpec(extensions=("py", "pyi"), command=("pyright",))
        self.go = LspServerSpec(extensions=("go",), command=("gopls",))
        self.servers = [self.py, self.go]

    def test_matches_by_extension(self) -> None:
        self.assertIs(resolve_server_for_path("foo/bar.py", self.servers), self.py)
        self.assertIs(resolve_server_for_path("foo/bar.go", self.servers), self.go)

    def test_returns_none_when_no_match(self) -> None:
        self.assertIsNone(resolve_server_for_path("foo/bar.txt", self.servers))

    def test_first_match_wins_on_overlap(self) -> None:
        # Two servers claiming .py — the first one in the list wins.
        ruff = LspServerSpec(extensions=("py",), command=("ruff",))
        self.assertIs(
            resolve_server_for_path("a.py", [ruff, self.py]),
            ruff,
        )

    def test_uppercase_extension_still_matches(self) -> None:
        self.assertIs(resolve_server_for_path("A.PY", self.servers), self.py)


class TestLoadEngineConfig(unittest.TestCase):
    def test_no_inputs_returns_defaults(self) -> None:
        cfg = load_engine_config()
        self.assertIsInstance(cfg, EngineConfig)
        self.assertEqual(cfg.preload.max_files, 200)
        self.assertTrue(cfg.preload.use_code_graph)
        self.assertFalse(cfg.compile_aware.enabled)
        self.assertEqual(cfg.session_locks.debounce_seconds, 30.0)
        self.assertEqual(cfg.session_locks.query_timeout_ms, 500)
        self.assertEqual(cfg.memory.max_files_per_lsp, 500)

    def test_global_block_only(self) -> None:
        cfg = load_engine_config(
            global_block={"preload": {"max_files": 50}, "compile_aware": {"enabled": True}},
        )
        self.assertEqual(cfg.preload.max_files, 50)
        self.assertTrue(cfg.preload.use_code_graph)  # untouched key keeps default
        self.assertTrue(cfg.compile_aware.enabled)

    def test_per_project_overrides_global(self) -> None:
        with TemporaryDirectory() as tmp:
            project_cfg = Path(tmp) / "lsp-engine.toml"
            project_cfg.write_text(
                "[preload]\n"
                "# 999 because monorepo — comment proves TOML is doing its job\n"
                "max_files = 999\n",
                encoding="utf-8",
            )
            cfg = load_engine_config(
                project_path=project_cfg,
                global_block={"preload": {"max_files": 50}, "compile_aware": {"enabled": True}},
            )
            # Project's max_files wins; global compile_aware survives.
            self.assertEqual(cfg.preload.max_files, 999)
            self.assertTrue(cfg.compile_aware.enabled)

    def test_compile_aware_commands_round_trip(self) -> None:
        cfg = load_engine_config(
            global_block={
                "compile_aware": {
                    "enabled": True,
                    "commands": {
                        "rs": ["cargo", "check", "--message-format=json"],
                        "ts": ["tsc", "--noEmit"],
                    },
                }
            },
        )
        self.assertEqual(
            cfg.compile_aware.commands["rs"],
            ("cargo", "check", "--message-format=json"),
        )
        self.assertEqual(cfg.compile_aware.commands["ts"], ("tsc", "--noEmit"))

    def test_invalid_project_toml_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "lsp-engine.toml"
            bad.write_text("[unclosed_table\n", encoding="utf-8")
            with self.assertRaises(CclspConfigError) as ctx:
                load_engine_config(project_path=bad)
            self.assertIn("invalid TOML", str(ctx.exception))

    def test_missing_project_file_falls_back_to_global(self) -> None:
        cfg = load_engine_config(
            project_path="/no/such/path.toml",
            global_block={"session_locks": {"debounce_seconds": 5.5}},
        )
        self.assertEqual(cfg.session_locks.debounce_seconds, 5.5)

    def test_toml_with_inline_compile_commands(self) -> None:
        """Reality check: a hand-edited TOML with comments parses
        cleanly and the comment annotations survive (they're stripped
        during parse but the values are intact)."""
        with TemporaryDirectory() as tmp:
            project_cfg = Path(tmp) / "lsp-engine.toml"
            project_cfg.write_text(
                "# disable compile_aware until rust-analyzer settles\n"
                "[compile_aware]\n"
                "enabled = false\n"
                "\n"
                "[compile_aware.commands]\n"
                "# match-format=json so we can structured-parse\n"
                'rs = ["cargo", "check", "--message-format=json"]\n'
                'ts = ["tsc", "--noEmit"]\n'
                "\n"
                "[session_locks]\n"
                "debounce_seconds = 15.0  # short because we ship single-session in P1\n",
                encoding="utf-8",
            )
            cfg = load_engine_config(project_path=project_cfg)
            self.assertFalse(cfg.compile_aware.enabled)
            self.assertEqual(
                cfg.compile_aware.commands["rs"],
                ("cargo", "check", "--message-format=json"),
            )
            self.assertEqual(cfg.session_locks.debounce_seconds, 15.0)


if __name__ == "__main__":
    unittest.main()
