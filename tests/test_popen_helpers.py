"""Tests for ``claude_hooks._popen.detach_kwargs`` (#221, 2026-05-19).

The helper consolidates the cross-platform incantation needed to
spawn a detached, windowless child. Before #221 the pattern was
re-rolled in seven spawn sites; three of them (consultants_forwarder,
store_async, code_graph builder) skipped the Windows flags entirely
and could pop a visible console on the user's desktop. The helper
plus a single audit pass closes that.

Coverage:

- POSIX path: ``start_new_session=True`` exactly, no Windows flag.
- Windows path: ``creationflags`` carries
  ``CREATE_NO_WINDOW | DETACHED_PROCESS``, no ``start_new_session``.
- Constants-absent path: ``getattr`` fallback returns 0, the OR
  evaluates to 0, and the call stays safe — important for
  stripped-down embedded Pythons that don't ship the constants.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from claude_hooks import _popen


class TestDetachKwargsPosix:
    """POSIX path — start_new_session=True exactly."""

    def test_posix_returns_session_flag(self):
        with patch.object(_popen, "os") as os_mod:
            os_mod.name = "posix"
            kw = _popen.detach_kwargs()
        assert kw == {"start_new_session": True}

    def test_posix_does_not_set_creationflags(self):
        with patch.object(_popen, "os") as os_mod:
            os_mod.name = "posix"
            kw = _popen.detach_kwargs()
        assert "creationflags" not in kw

    @pytest.mark.parametrize("posix_name", ["posix", "darwin"])
    def test_posix_variants(self, posix_name):
        """Any non-'nt' name falls through to POSIX behaviour."""
        with patch.object(_popen, "os") as os_mod:
            os_mod.name = posix_name
            kw = _popen.detach_kwargs()
        assert kw == {"start_new_session": True}


class TestDetachKwargsWindows:
    """Windows path — creationflags = CREATE_NO_WINDOW | DETACHED_PROCESS."""

    def test_windows_returns_creationflags(self):
        with patch.object(_popen, "os") as os_mod:
            os_mod.name = "nt"
            kw = _popen.detach_kwargs()
        assert "creationflags" in kw
        assert "start_new_session" not in kw

    def test_windows_flags_or_three_constants(self):
        # detach_kwargs ORs THREE constants on Windows (v1.10.6+):
        # CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB.
        # Stub all three on an *isolated* fake subprocess (patched onto
        # _popen) so the assertion is platform-independent. The old form
        # mutated the real subprocess module and omitted the breakaway
        # flag, so it passed on POSIX (where CREATE_BREAKAWAY_FROM_JOB is
        # absent → getattr returns 0) but failed on real Windows where the
        # product's breakaway bit is genuinely present.
        import types
        fake = types.SimpleNamespace(
            CREATE_NO_WINDOW=0x08000000,
            DETACHED_PROCESS=0x00000008,
            CREATE_BREAKAWAY_FROM_JOB=0x01000000,
        )
        with patch.object(_popen, "os") as os_mod, \
             patch.object(_popen, "subprocess", fake):
            os_mod.name = "nt"
            kw = _popen.detach_kwargs()
        assert kw["creationflags"] == (0x08000000 | 0x00000008 | 0x01000000)

    def test_windows_constants_absent_falls_back_to_zero(self):
        """Stripped Pythons may not have the constants — getattr
        returns 0 and the call stays safe (no AttributeError)."""
        import types
        fake = types.SimpleNamespace()  # no CREATE_NO_WINDOW / DETACHED_PROCESS
        with patch.object(_popen, "os") as os_mod, \
             patch.object(_popen, "subprocess", fake):
            os_mod.name = "nt"
            kw = _popen.detach_kwargs()
        # 0 | 0 == 0, but the key must still be present for callers
        # who unpack ``**kw``.
        assert kw == {"creationflags": 0}


class TestDetachKwargsCallableWithPopen:
    """Smoke: helper output unpacks cleanly into subprocess.Popen."""

    def test_unpacks_into_popen_call_signature(self):
        # Don't actually spawn; just verify that the kwargs dict shape
        # matches what subprocess.Popen accepts. If detach_kwargs ever
        # adds an unexpected key, this would fail on import inspection.
        kw = _popen.detach_kwargs()
        import inspect
        sig = inspect.signature(subprocess.Popen)
        for k in kw:
            assert k in sig.parameters, (
                f"{k!r} is not a known subprocess.Popen kwarg"
            )


# ─── v1.10.1 — windowless_python_executable ────────────────────────────
#
# Regression guard for the pandorum-visible LSP daemon window
# (2026-05-22). ``sys.executable`` on Windows in a conda env is
# ``python.exe`` (console-subsystem) — even with
# ``CREATE_NO_WINDOW | DETACHED_PROCESS``, Python can auto-allocate a
# console at interpreter startup. The helper swaps to ``pythonw.exe``
# (windows-subsystem) where available so long-lived daemons run
# truly windowless.


class TestWindowlessPythonExecutablePosix:
    """POSIX: ``pythonw.exe`` doesn't exist — return sys.executable unchanged."""

    def test_posix_returns_sys_executable_unchanged(self):
        import sys
        with patch.object(_popen, "os") as os_mod:
            os_mod.name = "posix"
            result = _popen.windowless_python_executable()
        assert result == sys.executable

    @pytest.mark.parametrize("posix_name", ["posix", "darwin", "linux"])
    def test_any_non_nt_returns_sys_executable(self, posix_name):
        import sys
        with patch.object(_popen, "os") as os_mod:
            os_mod.name = posix_name
            result = _popen.windowless_python_executable()
        assert result == sys.executable


class TestWindowlessPythonExecutableWindows:
    """Windows: swap python.exe → pythonw.exe when the sibling exists."""

    def test_swaps_python_exe_to_pythonw_when_sibling_exists(self, tmp_path):
        py = tmp_path / "python.exe"
        pyw = tmp_path / "pythonw.exe"
        py.touch()
        pyw.touch()
        with patch.object(_popen, "os") as os_mod, \
             patch.object(_popen, "sys") as sys_mod:
            os_mod.name = "nt"
            sys_mod.executable = str(py)
            result = _popen.windowless_python_executable()
        assert result == str(pyw)

    def test_returns_sys_executable_when_no_pythonw_sibling(self, tmp_path):
        """No pythonw.exe next to python.exe → fall back to python.exe.
        Stripped Python builds / custom embedded interpreters."""
        py = tmp_path / "python.exe"
        py.touch()
        # NB: do NOT create pythonw.exe
        with patch.object(_popen, "os") as os_mod, \
             patch.object(_popen, "sys") as sys_mod:
            os_mod.name = "nt"
            sys_mod.executable = str(py)
            result = _popen.windowless_python_executable()
        assert result == str(py)

    def test_idempotent_when_already_pythonw(self, tmp_path):
        """If sys.executable is already pythonw.exe (e.g. installer
        registered the conda env's pythonw), return it unchanged
        without poking the filesystem for another pythonw sibling."""
        pyw = tmp_path / "pythonw.exe"
        pyw.touch()
        with patch.object(_popen, "os") as os_mod, \
             patch.object(_popen, "sys") as sys_mod:
            os_mod.name = "nt"
            sys_mod.executable = str(pyw)
            result = _popen.windowless_python_executable()
        assert result == str(pyw)

    def test_case_insensitive_already_pythonw_check(self, tmp_path):
        """Windows filesystems are case-insensitive — ``PYTHONW.EXE``
        / ``Pythonw.exe`` should also count as already-windowless."""
        pyw = tmp_path / "Pythonw.exe"
        pyw.touch()
        with patch.object(_popen, "os") as os_mod, \
             patch.object(_popen, "sys") as sys_mod:
            os_mod.name = "nt"
            sys_mod.executable = str(pyw)
            result = _popen.windowless_python_executable()
        # Either accepts the input as already-windowless OR finds the
        # sibling pythonw.exe; both outcomes are correct (no visible
        # window). What we must NOT do is fail or return python.exe.
        assert "python.exe" not in result.lower() or \
               result.lower().endswith("pythonw.exe")


class TestSpawnSitesUseWindowlessExecutable:
    """Regression: the two Python-spawn sites must compose the helper
    into their command, not bypass it via sys.executable.

    These tests use source inspection (rather than live-mocking the
    Popen call) so they survive cross-platform — they assert the
    intent at the source level, which is invariant across Linux test
    hosts and Windows production hosts.
    """

    def test_lsp_engine_spawn_uses_windowless_helper(self):
        """``claude_hooks.lsp_engine.client._spawn_daemon`` must build
        its argv with ``windowless_python_executable()``, not
        ``sys.executable``. This is the user-visible regression from
        pandorum 2026-05-22 — the daemon's cmd.exe console flashed up
        and stayed open because ``sys.executable`` is ``python.exe``
        (console subsystem) in a conda env."""
        import inspect
        from claude_hooks.lsp_engine import client
        src = inspect.getsource(client._spawn_daemon)
        assert "windowless_python_executable()" in src, (
            "lsp_engine.client._spawn_daemon no longer composes "
            "windowless_python_executable() into its argv — the "
            "Windows visible-window bug will re-surface. See the "
            "v1.10.1 hot-fix and claude_hooks._popen for the helper."
        )
        # Belt-and-braces: also assert ``sys.executable`` is NOT the
        # cmd[0] entry. Any new call to sys.executable elsewhere in
        # the file is fine — we only care about the daemon argv.
        # We check the cmd = [...] block specifically.
        cmd_block = src[src.find("cmd = ["):src.find("]", src.find("cmd = ["))]
        assert "sys.executable" not in cmd_block, (
            "_spawn_daemon's cmd[] block still references "
            "sys.executable — that's the bug being guarded against."
        )

    def test_store_async_spawn_uses_windowless_helper(self):
        """``claude_hooks.store_async.spawn`` builds the same kind of
        detached Python child as the LSP daemon. Same fix, same guard.
        Short-lived but still visible on Windows; using
        ``windowless_python_executable`` keeps the surface uniform."""
        import inspect
        from claude_hooks import store_async
        src = inspect.getsource(store_async.spawn)
        assert "windowless_python_executable()" in src, (
            "store_async.spawn no longer composes "
            "windowless_python_executable() into its argv. Same bug "
            "class as the v1.10.1 LSP daemon fix — restore it."
        )

    def test_install_pgvector_setup_uses_pythonw_for_mcp(self):
        """install.py's pgvector MCP setup must bake the windowless
        interpreter into the launcher when running on Windows.

        Without this guard, the user-visible pandorum regression of
        2026-05-23 returns: Claude Code spawns the pgvector MCP child
        via the ``pgvector-mcp.cmd`` shim, which exec'd ``python.exe``
        (console-subsystem) and the interpreter auto-allocated a
        console window even with the parent passing
        ``windowsHide: true``. Same bug class as v1.10.1 / v1.10.4 —
        ``find_conda_env_python_for_mcp`` prefers ``pythonw.exe``."""
        import inspect
        import install
        src = inspect.getsource(install._setup_pgvector_mcp)
        assert "find_conda_env_python_for_mcp(" in src, (
            "install._setup_pgvector_mcp no longer resolves the "
            "launcher interpreter via find_conda_env_python_for_mcp — "
            "the Windows visible-window bug will re-surface on the "
            "next install. See find_conda_env_python_for_mcp's "
            "docstring for the rationale."
        )

    def test_install_sqlite_vec_setup_uses_pythonw_for_mcp(self):
        """Mirror of the pgvector guard. The sqlite_vec MCP launcher
        is the same shape (``.cmd`` shim → python.exe → -m
        claude_hooks.sqlite_vec_mcp) and inherits the same bug class."""
        import inspect
        import install
        src = inspect.getsource(install._setup_sqlite_vec_mcp)
        assert "find_conda_env_python_for_mcp(" in src, (
            "install._setup_sqlite_vec_mcp no longer resolves the "
            "launcher interpreter via find_conda_env_python_for_mcp. "
            "Restore it."
        )

    def test_find_conda_env_python_for_mcp_prefers_pythonw_on_windows(self):
        """Behavioural test for the helper itself: on Windows, when a
        ``pythonw.exe`` sibling exists, it must be preferred over
        ``python.exe``. On POSIX or when pythonw is missing, falls back
        cleanly to the plain interpreter.

        Uses ``PureWindowsPath`` / ``PurePosixPath`` sentinels so the
        test runs on any host (the real ``Path`` subclass for the other
        OS can't be instantiated cross-platform)."""
        import install
        from unittest.mock import patch
        from pathlib import PurePosixPath, PureWindowsPath

        sentinel_pyw = PureWindowsPath("C:/x/envs/y/pythonw.exe")
        sentinel_py_win = PureWindowsPath("C:/x/envs/y/python.exe")
        sentinel_py_posix = PurePosixPath("/home/u/anaconda3/envs/y/bin/python")

        # Windows + pythonw available -> returns pythonw.
        with patch.object(install.os, "name", "nt"), \
             patch.object(install, "find_conda_env_pythonw",
                          return_value=sentinel_pyw), \
             patch.object(install, "find_conda_env_python",
                          return_value=sentinel_py_win):
            assert install.find_conda_env_python_for_mcp() is sentinel_pyw

        # Windows + pythonw missing -> falls back to python.exe.
        with patch.object(install.os, "name", "nt"), \
             patch.object(install, "find_conda_env_pythonw",
                          return_value=None), \
             patch.object(install, "find_conda_env_python",
                          return_value=sentinel_py_win):
            assert install.find_conda_env_python_for_mcp() is sentinel_py_win

        # POSIX -> never tries pythonw, returns plain interpreter.
        with patch.object(install.os, "name", "posix"), \
             patch.object(install, "find_conda_env_python",
                          return_value=sentinel_py_posix):
            assert install.find_conda_env_python_for_mcp() is sentinel_py_posix
