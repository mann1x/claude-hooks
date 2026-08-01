"""v1.10.5 LSP-engine bug fixes — regression suite.

Two more bugs surfaced by the pandorum 2026-05-22 follow-up
session after v1.10.4:

F. **Visible msbuild console window** — the compile orchestrator's
   ``subprocess.run`` ran without ``CREATE_NO_WINDOW`` on Windows.
   The daemon itself is windowless (DETACHED_PROCESS at spawn
   time via ``client._spawn_daemon``), so when it subsequently
   spawned a console-subsystem child without that flag, Windows
   allocated a fresh console for the child — visible to the user
   as a popping cmd.exe window. The user's framing was load-
   bearing: this is a framework-level guarantee the engine must
   provide, not something callers (compile-aware toml authors,
   LSP-server contributors) should know to opt into. v1.10.5 adds
   :func:`silent_subprocess_kwargs` in ``claude_hooks._popen`` —
   the inline-friendly sibling of the existing :func:`detach_kwargs`
   helper that handles ``DETACHED_PROCESS`` for detached children.

G. **Shell-mangled --project silently no-ops** — running
   ``claude-hooks-lsp status --project "C:\\path"`` from a shell
   that mangles backslashes inside quotes produced a string like
   ``C:pathmangled`` that didn't exist on disk. ``Path.resolve()``
   then lexically completed it from cwd, and the CLI computed a
   hash for the phantom path. ``status`` quietly returned
   ``running: false`` (no daemon was listening at the bogus
   address), but ``restart`` ran ``stop`` (no daemon to stop) +
   ``cleanup`` (no dir to clean) and reported
   ``restarted: true`` — silent success masquerading as actual
   work, dangerous in any wrapper script. v1.10.5 validates that
   ``--project`` resolves to a real directory and bails with a
   diagnostic listing the known daemon state dirs (read from
   ``~/.claude/lsp-engine/*/project`` hint files) so the user
   can see what the correct invocation form would be.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# ──────────────────────────────────────────────────────────────────────
# Fix F — silent_subprocess_kwargs
# ──────────────────────────────────────────────────────────────────────


class TestSilentSubprocessKwargs(unittest.TestCase):
    """The shared helper must return ``CREATE_NO_WINDOW`` (only) on
    Windows and an empty dict on POSIX. Distinct from
    ``detach_kwargs`` because the compile path uses
    ``capture_output=True`` which is incompatible with
    ``DETACHED_PROCESS``."""

    def test_posix_returns_empty_dict(self):
        from claude_hooks import _popen
        with patch.object(_popen.os, "name", "posix"):
            self.assertEqual(_popen.silent_subprocess_kwargs(), {})

    def test_windows_returns_create_no_window_only(self):
        from claude_hooks import _popen
        with patch.object(_popen.os, "name", "nt"):
            kwargs = _popen.silent_subprocess_kwargs()
        self.assertIn("creationflags", kwargs)
        # Must NOT include DETACHED_PROCESS (which would break
        # capture_output). CREATE_NO_WINDOW is 0x08000000.
        expected = getattr(_popen.subprocess, "CREATE_NO_WINDOW", 0)
        self.assertEqual(kwargs["creationflags"], expected)
        # Belt-and-braces: explicitly assert DETACHED_PROCESS is NOT
        # OR'd in. If a future contributor adds it for symmetry with
        # detach_kwargs, this assertion catches it.
        detach = getattr(_popen.subprocess, "DETACHED_PROCESS", 0)
        if detach:
            self.assertEqual(kwargs["creationflags"] & detach, 0)

    def test_does_not_set_start_new_session_on_posix(self):
        """POSIX kwargs must be empty — ``start_new_session`` is only
        appropriate for ``detach_kwargs`` (long-lived background
        children), not for ``subprocess.run`` callers waiting on
        the child."""
        from claude_hooks import _popen
        with patch.object(_popen.os, "name", "posix"):
            kwargs = _popen.silent_subprocess_kwargs()
        self.assertNotIn("start_new_session", kwargs)


class TestCompileRunnerUsesSilentKwargs(unittest.TestCase):
    """``CompileRunner._run_once`` must pass the silent kwargs into
    its ``subprocess.run`` so the spawned compile child is
    windowless on Windows."""

    def test_source_imports_silent_subprocess_kwargs(self):
        import inspect
        from claude_hooks.lsp_engine.compile import CompileRunner
        src = inspect.getsource(CompileRunner._run_once)
        self.assertIn("silent_subprocess_kwargs", src, (
            "_run_once no longer wires silent_subprocess_kwargs into "
            "subprocess.run — a compile child on Windows will pop "
            "a visible console in front of the user."
        ))

    def test_subprocess_run_call_uses_kwargs(self):
        """The actual ``subprocess.run`` call must splat the helper's
        output. Source-inspection is enough — running a fake compile
        and asserting on creationflags is brittle across CPython
        versions that mangle the call signature."""
        import inspect
        from claude_hooks.lsp_engine.compile import CompileRunner
        src = inspect.getsource(CompileRunner._run_once)
        # Look for the splat in the subprocess.run call shape.
        self.assertIn("**silent_subprocess_kwargs()", src, (
            "_run_once invokes subprocess.run but no longer splats "
            "silent_subprocess_kwargs() into it — the windowless "
            "guarantee is gone. Re-add **silent_subprocess_kwargs()."
        ))


# ──────────────────────────────────────────────────────────────────────
# Fix G — --project must resolve to a real directory
# ──────────────────────────────────────────────────────────────────────


class TestValidateProjectPath(unittest.TestCase):
    """``_validate_project_path`` must accept existing dirs and
    reject anything else with a diagnostic that lists known
    daemon state dirs."""

    def test_existing_directory_returns_none(self):
        from claude_hooks.lsp_engine.__main__ import _validate_project_path
        with TemporaryDirectory() as td:
            self.assertIsNone(_validate_project_path(td))

    def test_nonexistent_path_returns_error_string(self):
        from claude_hooks.lsp_engine.__main__ import _validate_project_path
        err = _validate_project_path("/definitely/does/not/exist/anywhere")
        self.assertIsNotNone(err)
        self.assertIn("does not point to an existing directory", err)

    def test_existing_file_not_dir_returns_error(self):
        from claude_hooks.lsp_engine.__main__ import _validate_project_path
        with TemporaryDirectory() as td:
            f = Path(td) / "not-a-dir.txt"
            f.write_text("x", encoding="utf-8")
            err = _validate_project_path(str(f))
            self.assertIsNotNone(err)
            self.assertIn("does not point to an existing directory", err)

    def test_error_message_includes_shell_escape_hint(self):
        """Most common cause of this error is cmd.exe eating
        backslashes inside quoted args. Surface that in the message
        so the user doesn't waste time chasing other theories."""
        from claude_hooks.lsp_engine.__main__ import _validate_project_path
        err = _validate_project_path("/nope") or ""
        self.assertIn("shell escaping", err.lower())
        self.assertIn("cmd.exe", err.lower())

    def test_error_message_lists_known_daemon_state_dirs(self):
        """When state dirs exist with project hint files, surface
        them so the user can see the resolved-path form that the
        live daemon (if any) was started with."""
        from claude_hooks.lsp_engine.__main__ import _validate_project_path
        with TemporaryDirectory() as td:
            tdp = Path(td)
            fake_home = tdp / "home"
            (fake_home / ".claude" / "lsp-engine").mkdir(parents=True)
            state_a = fake_home / ".claude" / "lsp-engine" / "abc123def456"
            state_a.mkdir()
            (state_a / "project").write_text(
                "/real/path/to/projectA\n", encoding="utf-8",
            )
            state_b = fake_home / ".claude" / "lsp-engine" / "999888777666"
            state_b.mkdir()
            (state_b / "project").write_text(
                "/real/path/to/projectB\n", encoding="utf-8",
            )
            with patch.object(Path, "home", return_value=fake_home):
                err = _validate_project_path("/nope/not/here") or ""
            self.assertIn("Known daemon state dirs", err)
            self.assertIn("abc123def456", err)
            self.assertIn("/real/path/to/projectA", err)
            self.assertIn("999888777666", err)
            self.assertIn("/real/path/to/projectB", err)


class TestMainRejectsMangledProject(unittest.TestCase):
    """``main()`` must short-circuit with exit code 2 when
    ``--project`` doesn't point at a real directory — for every
    subcommand EXCEPT ``daemon`` (which legitimately may create
    the dir when first spawned)."""

    def test_status_rejects_nonexistent_project(self):
        from claude_hooks.lsp_engine.__main__ import main
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            rc = main(["status", "--project", "/does/not/exist"])
        self.assertEqual(rc, 2)
        self.assertIn("does not point to an existing directory", buf.getvalue())

    def test_stop_rejects_nonexistent_project(self):
        from claude_hooks.lsp_engine.__main__ import main
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            rc = main(["stop", "--project", "/does/not/exist"])
        self.assertEqual(rc, 2)

    def test_cleanup_rejects_nonexistent_project(self):
        """Cleanup is destructive — it must NOT proceed against a
        phantom path. Pre-fix it would silently remove a state dir
        for whatever (correct) hash happened to collide."""
        from claude_hooks.lsp_engine.__main__ import main
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            rc = main(["cleanup", "--project", "/does/not/exist"])
        self.assertEqual(rc, 2)

    def test_restart_rejects_nonexistent_project(self):
        """The dangerous case from the pandorum diagnosis: pre-fix
        ``restart`` reported ``restarted: true`` for a mangled path
        while doing nothing. Now: hard error, exit 2."""
        from claude_hooks.lsp_engine.__main__ import main
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            rc = main(["restart", "--project", "/does/not/exist"])
        self.assertEqual(rc, 2)
        # The misleading success payload must NOT be on stdout.
        # (We don't capture stdout here, but the buf has our error.)
        self.assertIn("does not point to an existing directory", buf.getvalue())

    def test_status_accepts_existing_project(self):
        """Sanity check: existing dirs still flow through to the
        actual handler (which will return running:false here since
        no daemon is up)."""
        from claude_hooks.lsp_engine.__main__ import main
        with TemporaryDirectory() as td:
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = main([
                    "status", "--project", td,
                    "--state-base", str(Path(td) / "state"),
                ])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertFalse(payload["running"])

    def test_daemon_subcommand_allowed_to_create_missing_dir(self):
        """The ``daemon`` subcommand path is the one place where a
        non-existing project dir is conceivable (e.g., calling the
        daemon manually with --project before mkdir-ing the dir).
        We don't want to break that flow by overzealous validation.
        Just verify the validation isn't applied — we don't run the
        full daemon since that'd block."""
        # The validation is only called in main() conditional on
        # ``args.subcommand != "daemon"``. Confirm via source-
        # inspection that the guard exists.
        import inspect
        from claude_hooks.lsp_engine.__main__ import main
        src = inspect.getsource(main)
        self.assertIn('args.subcommand != "daemon"', src, (
            "main() no longer skips _validate_project_path for the "
            "daemon subcommand — manual daemon spawns with a not-yet-"
            "existing project dir will be rejected."
        ))


if __name__ == "__main__":
    unittest.main()
