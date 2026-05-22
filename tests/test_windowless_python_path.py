"""v1.10.4 regression: ``windowless_python_path`` + consultants forwarder
and code_graph spawn sites must swap ``python.exe`` → ``pythonw.exe``
before invoking ``subprocess.Popen``.

Bug: ``CREATE_NO_WINDOW | DETACHED_PROCESS`` does NOT prevent
``python.exe`` (console-subsystem binary) from self-allocating a
console window at interpreter startup. The user observed a visible
"consultants" Python console window on the desktop despite the flags
being set on the forwarder's Popen call. The fix mirrors v1.10.1's
LSP-engine treatment: swap the interpreter to ``pythonw.exe`` if a
sibling exists.

Audit (2026-05-22) — full list of production Popen sites: every
external-binary spawn (llamafile, axon, gitnexus, claudemem,
LSP servers, episodic-memory) is already windowless-safe via
``CREATE_NO_WINDOW``. Every Python-interpreter spawn must route the
``cmd[0]`` through ``windowless_python_executable`` (for sys.executable)
or ``windowless_python_path`` (for configured paths). Pre-v1.10.4 the
forwarder and code_graph were the only outstanding offenders.
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


class TestWindowlessPythonPath(unittest.TestCase):
    """``windowless_python_path`` rewrites configured interpreter paths."""

    def test_posix_passthrough(self):
        from claude_hooks._popen import windowless_python_path
        with patch.object(os, "name", "posix"):
            self.assertEqual(
                windowless_python_path("/opt/conda/envs/foo/bin/python"),
                "/opt/conda/envs/foo/bin/python",
            )

    def test_empty_input_passthrough(self):
        from claude_hooks._popen import windowless_python_path
        with patch.object(os, "name", "nt"):
            self.assertEqual(windowless_python_path(""), "")

    def test_windows_pythonw_unchanged(self):
        from claude_hooks._popen import windowless_python_path
        with patch.object(os, "name", "nt"):
            self.assertEqual(
                windowless_python_path(r"C:\envs\foo\pythonw.exe"),
                r"C:\envs\foo\pythonw.exe",
            )

    def test_windows_python_with_sibling_rewrites(self):
        from claude_hooks._popen import windowless_python_path
        with tempfile.TemporaryDirectory() as td:
            envroot = Path(td)
            (envroot / "python.exe").write_text("stub")
            pyw = envroot / "pythonw.exe"
            pyw.write_text("stub")
            with patch.object(os, "name", "nt"):
                out = windowless_python_path(str(envroot / "python.exe"))
            self.assertEqual(Path(out).name.lower(), "pythonw.exe")
            self.assertEqual(Path(out).parent, envroot)

    def test_windows_python_without_sibling_passthrough(self):
        from claude_hooks._popen import windowless_python_path
        with tempfile.TemporaryDirectory() as td:
            envroot = Path(td)
            (envroot / "python.exe").write_text("stub")
            # No pythonw.exe alongside.
            with patch.object(os, "name", "nt"):
                out = windowless_python_path(str(envroot / "python.exe"))
            self.assertEqual(Path(out).name.lower(), "python.exe")

    def test_unrelated_interpreter_name_passthrough(self):
        from claude_hooks._popen import windowless_python_path
        with patch.object(os, "name", "nt"):
            # Don't try to guess windowless variants of unknown names.
            self.assertEqual(
                windowless_python_path(r"C:\envs\foo\python3.11.exe"),
                r"C:\envs\foo\python3.11.exe",
            )


class TestConsultantsForwarderUsesWindowless(unittest.TestCase):
    """``EngineManager._ensure_engine`` must route through
    ``windowless_python_path`` and ``popen_detached`` so it can never
    spawn a visible console on Windows."""

    def test_source_imports_and_uses_helpers(self):
        from claude_hooks import consultants_forwarder
        src = inspect.getsource(consultants_forwarder.EngineManager)
        self.assertIn("windowless_python_path", src, (
            "EngineManager no longer routes engine_python through "
            "windowless_python_path — a visible Python console will "
            "pop up on Windows whenever the registered engine_python "
            "points at python.exe."
        ))
        self.assertIn("popen_detached", src, (
            "EngineManager no longer uses popen_detached — the engine "
            "subprocess loses CREATE_BREAKAWAY_FROM_JOB and dies when "
            "the daemon parent exits in a job-managed environment."
        ))

    def test_source_no_longer_passes_raw_engine_python(self):
        """Guard against a regression that re-introduces
        ``self.cfg.engine_python`` directly into the cmd list."""
        from claude_hooks import consultants_forwarder
        src = inspect.getsource(consultants_forwarder.EngineManager.ensure_running)
        # We expect a local rebinding before the cmd list:
        #   engine_python = windowless_python_path(self.cfg.engine_python)
        #   cmd = [engine_python, "-m", ...]
        # If the rebinding is removed, the cmd will reference
        # ``self.cfg.engine_python`` directly and the bug returns.
        self.assertIn("engine_python = windowless_python_path", src)


class TestCodeGraphSpawnUsesWindowless(unittest.TestCase):
    """``code_graph.__main__.build_async`` must spawn via the
    windowless python helper, not raw ``sys.executable``."""

    def test_source_uses_windowless_python_executable(self):
        from claude_hooks.code_graph import __main__ as cg_main
        src = inspect.getsource(cg_main)
        self.assertIn("windowless_python_executable", src, (
            "code_graph build_async no longer routes the spawn through "
            "windowless_python_executable — Python builder will flash "
            "a visible console on Windows when triggered from hooks."
        ))
        # The legacy ``sys.executable`` should NOT be the cmd[0] of
        # the Popen spawn anymore. We tolerate the import staying.
        # Find the Popen call and verify cmd[0] is not sys.executable.
        # Cheap text check is sufficient as a regression guard.
        spawn_idx = src.find("subprocess.Popen(")
        self.assertGreater(spawn_idx, 0)
        tail = src[spawn_idx:spawn_idx + 400]
        self.assertNotIn("[sys.executable,", tail, (
            "build_async cmd[0] is still sys.executable — that's "
            "python.exe on Windows and will self-allocate a console."
        ))


if __name__ == "__main__":
    unittest.main()
