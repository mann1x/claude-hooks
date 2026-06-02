"""Task #111 — CLI argparse + handler tests for ``config coder``.

Exercises the four new subcommands:
  config coder list
  config coder set <lang> --primary X [--fallback Y]
  config coder unset <lang>
  config coder set-default --primary X [--fallback Y]

Doesn't touch the network — each handler patches
``consultants.config`` so the test runs with no filesystem mutation.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from consultants import config as cc
from consultants.cli import (
    build_parser,
    cmd_config_coder_list,
    cmd_config_coder_set,
    cmd_config_coder_set_default,
    cmd_config_coder_unset,
)


class TestParserWiring(unittest.TestCase):
    """The argparse tree must dispatch to the right handler per
    verb. A regression here makes the CLI a silent no-op."""

    def setUp(self):
        self.parser = build_parser()

    def test_list_verb(self):
        args = self.parser.parse_args(["config", "coder", "list"])
        self.assertIs(args.fn, cmd_config_coder_list)
        self.assertEqual(args.coder_cmd, "list")

    def test_set_verb_full(self):
        args = self.parser.parse_args([
            "config", "coder", "set", "csharp",
            "--primary", "pro:cloud", "--fallback", "kimi:cloud",
        ])
        self.assertIs(args.fn, cmd_config_coder_set)
        self.assertEqual(args.language, "csharp")
        self.assertEqual(args.primary, "pro:cloud")
        self.assertEqual(args.fallback, "kimi:cloud")
        self.assertFalse(args.project)

    def test_set_verb_primary_only(self):
        args = self.parser.parse_args([
            "config", "coder", "set", "python", "--primary", "glm:cloud",
        ])
        self.assertEqual(args.primary, "glm:cloud")
        # fallback default is None (NOT empty string) so the
        # mutator's "keep current" branch fires on update.
        self.assertIsNone(args.fallback)

    def test_set_verb_fallback_clear_empty_string(self):
        args = self.parser.parse_args([
            "config", "coder", "set", "python", "--fallback", "",
        ])
        # Empty string is the explicit-clear sentinel; preserve it
        # all the way through.
        self.assertEqual(args.fallback, "")

    def test_unset_verb(self):
        args = self.parser.parse_args(["config", "coder", "unset", "go"])
        self.assertIs(args.fn, cmd_config_coder_unset)
        self.assertEqual(args.language, "go")

    def test_set_default_verb(self):
        args = self.parser.parse_args([
            "config", "coder", "set-default",
            "--primary", "glm:cloud", "--fallback", "kimi:cloud",
        ])
        self.assertIs(args.fn, cmd_config_coder_set_default)
        self.assertEqual(args.primary, "glm:cloud")
        self.assertEqual(args.fallback, "kimi:cloud")

    def test_project_flag(self):
        args = self.parser.parse_args([
            "config", "coder", "set", "python",
            "--primary", "x:cloud", "--project", "--cwd", "/tmp",
        ])
        self.assertTrue(args.project)
        self.assertEqual(args.cwd, "/tmp")

    def test_missing_verb_errors(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["config", "coder"])


class TestHandlers(unittest.TestCase):
    """Each handler is a thin adapter over the cc.* mutators —
    tests confirm the right mutator is called with the right kwargs
    AND that the JSON output is well-formed.
    """

    def _run(self, fn, args_dict, stdout):
        """Build an argparse.Namespace from args_dict and call fn.
        Returns the exit code."""
        import argparse
        args = argparse.Namespace(**args_dict)
        with mock.patch("sys.stdout", stdout):
            return fn(args, "base")

    def test_list_pretty_prints_block(self):
        captured = io.StringIO()
        # Isolate cwd + user config so the handler resolves to the
        # seeded defaults, not whatever per-project ``.claude-hooks/
        # consultants.toml`` happens to sit at the runner's cwd
        # (bug-664: an ``override_user_global=on`` project file leaks
        # in and flips both the values and the write scope).
        with self._isolated_config():
            rc = self._run(
                cmd_config_coder_list,
                {"cwd": None}, captured,
            )
        self.assertEqual(rc, 0)
        body = json.loads(captured.getvalue())
        self.assertTrue(body["ok"])
        self.assertIn("coder", body)
        self.assertIn("default_route", body["coder"])
        self.assertIn("routes_by_language", body["coder"])
        # Seeded defaults present:
        self.assertEqual(
            body["coder"]["default_route"]["primary"], "glm-5.1:cloud",
        )
        self.assertEqual(
            body["coder"]["routes_by_language"]["csharp"]["primary"],
            "deepseek-v4-pro:cloud",
        )

    def test_set_invokes_set_coder_route(self):
        with mock.patch.object(cc, "set_coder_route",
                                wraps=cc.set_coder_route) as m:
            with self._isolated_config():
                rc = self._run(
                    cmd_config_coder_set,
                    {"language": "typescript",
                     "primary": "qwen3:cloud",
                     "fallback": "kimi:cloud",
                     "project": False, "cwd": None},
                    io.StringIO(),
                )
        self.assertEqual(rc, 0)
        m.assert_called_once()
        kwargs = m.call_args.kwargs
        self.assertEqual(m.call_args.args[0], "typescript")
        self.assertEqual(kwargs["primary"], "qwen3:cloud")
        self.assertEqual(kwargs["fallback"], "kimi:cloud")
        self.assertEqual(kwargs["scope"], "user")

    def test_set_validation_error_returns_code_2(self):
        from consultants.cli import CLIError
        with self._isolated_config():
            with self.assertRaises(CLIError) as cm:
                self._run(
                    cmd_config_coder_set,
                    {"language": "BAD SLUG",
                     "primary": "x:cloud", "fallback": None,
                     "project": False, "cwd": None},
                    io.StringIO(),
                )
        self.assertEqual(cm.exception.exit_code, 2)

    def test_unset_invokes_unset_coder_route(self):
        with mock.patch.object(cc, "unset_coder_route",
                                wraps=cc.unset_coder_route) as m:
            with self._isolated_config():
                rc = self._run(
                    cmd_config_coder_unset,
                    {"language": "python",
                     "project": False, "cwd": None},
                    io.StringIO(),
                )
        self.assertEqual(rc, 0)
        m.assert_called_once_with("python", scope="user", cwd=None)

    def test_set_default_invokes_set_coder_default_route(self):
        with mock.patch.object(cc, "set_coder_default_route",
                                wraps=cc.set_coder_default_route) as m:
            with self._isolated_config():
                rc = self._run(
                    cmd_config_coder_set_default,
                    {"primary": "kimi:cloud", "fallback": "glm:cloud",
                     "project": False, "cwd": None},
                    io.StringIO(),
                )
        self.assertEqual(rc, 0)
        m.assert_called_once_with(
            primary="kimi:cloud", fallback="glm:cloud",
            scope="user", cwd=None,
        )

    def test_project_scope_passes_cwd(self):
        with mock.patch.object(cc, "set_coder_route",
                                wraps=cc.set_coder_route) as m:
            with self._isolated_config():
                rc = self._run(
                    cmd_config_coder_set,
                    {"language": "rust",
                     "primary": "x:cloud", "fallback": "y:cloud",
                     "project": True, "cwd": "/tmp"},
                    io.StringIO(),
                )
        self.assertEqual(rc, 0)
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs["scope"], "project")
        self.assertEqual(kwargs["cwd"], Path("/tmp").resolve())

    # ----- helpers ---------------------------------------------- #

    def _isolated_config(self):
        """Context manager: redirect user_config_path to a temp file
        so the mutators don't touch the real home dir."""
        return _IsolatedConfig()


class _IsolatedConfig:
    """Redirect *both* config-resolution inputs to a throwaway dir:

    1. ``user_config_path`` → a temp file (so user-scope writes never
       touch the real home dir), and
    2. **the process cwd** → the same empty temp dir, so the
       per-project lookup finds no ``.claude-hooks/consultants.toml``.

    Without (2) a real per-project config sitting at the runner's cwd
    (e.g. this repo's own ``.claude-hooks/consultants.toml`` with
    ``override_user_global = true``) leaks into the handler: it flips
    the auto write-scope from ``user`` to ``project`` *and* the
    ``wraps=``-real mutator tests then clobber that live file
    (bug-664). Isolating cwd makes the tests host-independent and
    side-effect-free.
    """

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self._patcher = mock.patch.object(
            cc, "user_config_path",
            return_value=Path(self._tmp.name) / "consultants.toml",
        )
        self._patcher.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._patcher.stop()
        os.chdir(self._old_cwd)
        self._tmp.cleanup()
        return False


if __name__ == "__main__":
    unittest.main()
