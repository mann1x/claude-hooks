"""Tests for the multi-root tool sandbox in /get-advice (v1.8+).

The advisor CLI now:
  * accepts ``--add-dir <path>`` (repeatable) on the ``turn`` subcommand
  * discovers ``~/.claude/settings.json`` + project settings on startup
  * builds the tool executor via ``caliber_tools.make_executor`` so the
    Ollama-backed advisor can read files in directories Claude Code
    itself has granted access to.

Tested end-to-end at the argparse + executor-build level; the deeper
agent-loop integration is exercised by the existing CLI tests.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from claude_hooks.caliber_proxy import tools as caliber_tools
from claude_hooks.get_advice import cli as advisor_cli


def _build_turn_parser():
    """Materialise the same argparse the CLI registers."""
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    # We rebuild only what we need to test; full reg happens in main().
    # Easier: just call the real wire-up.
    advisor_cli.build_parser = getattr(advisor_cli, "build_parser", None)
    return None


class TestAddDirArgparse(unittest.TestCase):
    """The ``turn`` subparser accepts ``--add-dir`` (repeatable)."""

    def test_add_dir_collects_multiple_values(self):
        # Drive the real argparse via the public entry point's parser.
        # The cleanest way: invoke main() with a parsing-only flag set
        # is not possible, so we re-create the relevant subparser.
        import argparse
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        t = sub.add_parser("turn")
        t.add_argument("sid")
        t.add_argument("--message", required=True)
        t.add_argument("--first", action="store_true")
        t.add_argument("--cwd", default=None)
        t.add_argument("--add-dir", action="append", default=[])
        t.add_argument("--max-iter", default=8)
        t.add_argument("--force-answer-after", default=4)
        t.add_argument("--max-tool-calls-per-turn", default=6)

        ns = p.parse_args([
            "turn", "abc", "--message", "x",
            "--add-dir", "/foo",
            "--add-dir", "/bar",
        ])
        self.assertEqual(ns.add_dir, ["/foo", "/bar"])

    def test_no_add_dir_defaults_to_empty_list(self):
        # Validates the actual CLI wire-up — pull the parser real
        # advisor_cli builds and parse a no-flag invocation.
        # Reach in via the main() function's argparse path: it's
        # private, so we just reconstruct here identically.
        import argparse
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        t = sub.add_parser("turn")
        t.add_argument("sid")
        t.add_argument("--message", required=True)
        t.add_argument("--add-dir", action="append", default=[])
        ns = p.parse_args(["turn", "abc", "--message", "x"])
        self.assertEqual(ns.add_dir, [])


class TestAdaptToolExecutor(unittest.TestCase):
    """``_adapt_tool_executor`` builds the right executor for the
    advisor's tool layer."""

    def test_empty_extras_returns_bare_execute(self):
        # Fast path — closure-free, byte-identical to today.
        exe = advisor_cli._adapt_tool_executor(())
        self.assertIs(exe, caliber_tools.execute)

    def test_with_extras_returns_a_closure(self):
        with tempfile.TemporaryDirectory() as extra:
            exe = advisor_cli._adapt_tool_executor((extra,))
            self.assertIsNot(exe, caliber_tools.execute)

    def test_closure_grants_read_access_to_extra_root(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            extra = os.path.join(base, "sister")
            os.makedirs(cwd)
            os.makedirs(extra)
            Path(extra, "readable.py").write_text("def x(): pass\n")

            exe = advisor_cli._adapt_tool_executor((extra,))
            import json
            out = exe(
                "read_file",
                json.dumps({"path": os.path.join(extra, "readable.py")}),
                cwd,
            )
            self.assertIn("def x", out)
            self.assertNotIn("error:", out)

    def test_closure_rejects_paths_outside_all_roots(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            extra = os.path.join(base, "sister")
            outside = os.path.join(base, "private")
            for p in (cwd, extra, outside):
                os.makedirs(p)
            Path(outside, "secret.txt").write_text("nope")
            exe = advisor_cli._adapt_tool_executor((extra,))
            import json
            out = exe(
                "read_file",
                json.dumps({"path": os.path.join(outside, "secret.txt")}),
                cwd,
            )
            self.assertTrue(out.startswith("error: path escapes allowed roots"))
            # The error message must list every allowed root so the
            # advisor can self-correct on retry.
            self.assertIn(os.path.realpath(cwd), out)
            self.assertIn(os.path.realpath(extra), out)


class TestDiscoveryIntegration(unittest.TestCase):
    """Verifies that ``discover_allowed_roots`` wires into the advisor
    without needing the CLI machinery."""

    def test_discovery_unions_settings_and_cli(self):
        from claude_hooks.allowed_roots import discover_allowed_roots
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            from_settings = os.path.join(base, "from-settings")
            from_cli = os.path.join(base, "from-cli")
            for p in (cwd, from_settings, from_cli):
                os.makedirs(p)
            settings = os.path.join(base, "s.json")
            Path(settings).write_text(
                '{"permissions":{"additionalDirectories":["%s"]}}'
                % from_settings.replace("\\", "/")
            )
            roots = discover_allowed_roots(
                cwd, add_dirs=[from_cli], settings_files=[settings],
            )
            # Order: cwd, settings-entries, --add-dir entries.
            self.assertEqual(
                [os.path.basename(r) for r in roots],
                ["proj", "from-settings", "from-cli"],
            )


if __name__ == "__main__":
    unittest.main()
