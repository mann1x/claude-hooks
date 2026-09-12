"""v1.10.6 ``CREATE_BREAKAWAY_FROM_JOB`` fix — regression suite.

Bug J: ``DETACHED_PROCESS | CREATE_NO_WINDOW`` alone is **not**
enough on Windows to keep a child alive across parent exit. The
flags handle console inheritance and console allocation; they do
NOT detach from a Windows job object. When the parent
participates in a job (SSH/cmd invocations frequently do, even
when ``IsProcessInJob`` returns False — the OS applies implicit
process-tree cleanup), the job's terminate-on-parent-exit policy
kills every child including the daemon we just spawned.

Live bench on pandorum 2026-05-22:
- pre-fix: daemon at PID 81048 alive at t=2s in-script; **gone
  within 0.5 s of parent script exit** (no entry in process list).
- post-fix (CREATE_BREAKAWAY_FROM_JOB added): daemon at PID
  75036 alive at t=2s in-script, still alive at t=4s post-exit,
  ``status`` RPC responds with ``running:true``.

This test file source-inspects the helper rather than reproducing
the live bench (which requires a Windows host with a non-trivial
job hierarchy — pytest on POSIX wouldn't trip the bug to begin
with). The pandorum smoke is the empirical proof; these tests
are the regression guard.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


class TestDetachKwargsIncludesBreakaway(unittest.TestCase):
    """``detach_kwargs()`` must include ``CREATE_BREAKAWAY_FROM_JOB``
    on Windows alongside the existing ``DETACHED_PROCESS |
    CREATE_NO_WINDOW``."""

    def test_windows_creationflags_contain_breakaway(self):
        """Patch the Windows-only subprocess constants so the test
        runs on POSIX pytest. The constants live on the
        ``subprocess`` module on Windows but are absent elsewhere
        — production code uses ``getattr(..., 0)`` to guard."""
        from claude_hooks import _popen
        with patch.object(_popen.os, "name", "nt"), \
             patch.object(_popen.subprocess, "CREATE_NO_WINDOW",
                          0x08000000, create=True), \
             patch.object(_popen.subprocess, "DETACHED_PROCESS",
                          0x00000008, create=True), \
             patch.object(_popen.subprocess, "CREATE_BREAKAWAY_FROM_JOB",
                          0x01000000, create=True):
            kwargs = _popen.detach_kwargs()
        flags = kwargs["creationflags"]
        self.assertTrue(
            flags & 0x01000000,
            (
                "detach_kwargs no longer ORs in CREATE_BREAKAWAY_FROM_JOB. "
                "On Windows, daemons spawned from a job-managed parent "
                "(SSH/cmd/sandbox invocations) will be terminated when "
                "the parent exits — pandorum's daemon-died-on-parent-exit "
                "bug returns."
            ),
        )

    def test_windows_creationflags_still_contain_detached_and_no_window(self):
        """Adding BREAKAWAY must not have displaced the existing
        DETACHED_PROCESS / CREATE_NO_WINDOW bits."""
        from claude_hooks import _popen
        with patch.object(_popen.os, "name", "nt"), \
             patch.object(_popen.subprocess, "CREATE_NO_WINDOW",
                          0x08000000, create=True), \
             patch.object(_popen.subprocess, "DETACHED_PROCESS",
                          0x00000008, create=True), \
             patch.object(_popen.subprocess, "CREATE_BREAKAWAY_FROM_JOB",
                          0x01000000, create=True):
            flags = _popen.detach_kwargs()["creationflags"]
        self.assertTrue(flags & 0x00000008)
        self.assertTrue(flags & 0x08000000)
        self.assertTrue(flags & 0x01000000)

    def test_posix_unchanged(self):
        from claude_hooks import _popen
        with patch.object(_popen.os, "name", "posix"):
            self.assertEqual(_popen.detach_kwargs(), {"start_new_session": True})


class TestPopenDetached(unittest.TestCase):
    """``popen_detached`` wraps Popen with detach_kwargs and falls
    back to no-BREAKAWAY when the parent's job refuses it."""

    def test_posix_just_works(self):
        """On POSIX, ``popen_detached`` should pass through to a
        regular Popen with ``start_new_session=True``."""
        from claude_hooks import _popen
        captured_kwargs = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured_kwargs.update(kwargs)
                self.pid = 12345
        with patch.object(_popen.os, "name", "posix"), \
             patch.object(_popen.subprocess, "Popen", FakePopen):
            proc = _popen.popen_detached(["/bin/true"])
        self.assertTrue(captured_kwargs.get("start_new_session"))
        self.assertEqual(proc.pid, 12345)

    def test_windows_applies_breakaway_via_detach_kwargs(self):
        """Source-inspection: the helper must call ``detach_kwargs``
        and apply its creationflags to the Popen invocation."""
        import inspect
        from claude_hooks import _popen
        src = inspect.getsource(_popen.popen_detached)
        self.assertIn("detach_kwargs()", src)
        self.assertIn("creationflags", src)

    def test_falls_back_on_access_denied_breakaway(self):
        """When CreateProcess returns ERROR_ACCESS_DENIED (winerror
        5) because the parent's job rejects breakaway, the helper
        must retry without the BREAKAWAY bit so the daemon still
        spawns (lifetime-bound to parent in that case, but at least
        present)."""
        from claude_hooks import _popen

        attempts: list[int] = []
        # Build a fake OSError that looks like Windows
        # ERROR_ACCESS_DENIED from CreateProcess.
        first_fail = OSError(13, "Access denied")
        first_fail.winerror = 5

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                attempts.append(kwargs.get("creationflags", 0))
                # First call: raise. Second call: succeed.
                if len(attempts) == 1:
                    raise first_fail
                self.pid = 999
        with patch.object(_popen.os, "name", "nt"), \
             patch.object(_popen.subprocess, "CREATE_NO_WINDOW",
                          0x08000000, create=True), \
             patch.object(_popen.subprocess, "DETACHED_PROCESS",
                          0x00000008, create=True), \
             patch.object(_popen.subprocess, "CREATE_BREAKAWAY_FROM_JOB",
                          0x01000000, create=True), \
             patch.object(_popen.subprocess, "Popen", FakePopen):
            proc = _popen.popen_detached(["python"])
        breakaway = 0x01000000
        self.assertEqual(len(attempts), 2, "fallback retry did not fire")
        # First attempt had the BREAKAWAY bit; second did not.
        self.assertTrue(attempts[0] & breakaway)
        self.assertFalse(attempts[1] & breakaway)
        self.assertEqual(proc.pid, 999)

    def test_does_not_swallow_unrelated_oserror(self):
        """Only ACCESS_DENIED with the BREAKAWAY bit set should
        trigger the fallback. Other OSErrors propagate."""
        from claude_hooks import _popen
        unrelated = FileNotFoundError(2, "no such file")
        # No winerror attribute → fallback path requires winerror == 5,
        # so this should NOT retry.

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                raise unrelated

        with patch.object(_popen.os, "name", "nt"), \
             patch.object(_popen.subprocess, "Popen", FakePopen):
            with self.assertRaises(FileNotFoundError):
                _popen.popen_detached(["python"])

    def test_caller_supplied_creationflags_preserved(self):
        """When the caller passes their own ``creationflags`` (e.g.
        a debugging flag), they must be OR'd with the detach flags,
        not overwritten."""
        from claude_hooks import _popen
        captured = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured.update(kwargs)
                self.pid = 7
        custom = 0x00800000  # arbitrary flag (CREATE_PROTECTED_PROCESS)
        with patch.object(_popen.os, "name", "nt"), \
             patch.object(_popen.subprocess, "CREATE_NO_WINDOW",
                          0x08000000, create=True), \
             patch.object(_popen.subprocess, "DETACHED_PROCESS",
                          0x00000008, create=True), \
             patch.object(_popen.subprocess, "CREATE_BREAKAWAY_FROM_JOB",
                          0x01000000, create=True), \
             patch.object(_popen.subprocess, "Popen", FakePopen):
            _popen.popen_detached(["python"], creationflags=custom)
        flags = captured["creationflags"]
        self.assertTrue(flags & custom, "caller's flag was clobbered")
        self.assertTrue(flags & 0x01000000)


class TestSpawnDaemonUsesPopenDetached(unittest.TestCase):
    """``client._spawn_daemon`` must route through ``popen_detached``
    so the lsp-engine daemon gets the BREAKAWAY treatment + fallback."""

    def test_source_uses_popen_detached(self):
        import inspect
        from claude_hooks.lsp_engine.client import _spawn_daemon
        src = inspect.getsource(_spawn_daemon)
        self.assertIn("popen_detached(", src, (
            "_spawn_daemon no longer routes through popen_detached. "
            "The daemon will lose CREATE_BREAKAWAY_FROM_JOB and the "
            "pandorum daemon-dies-on-parent-exit bug returns."
        ))

    def test_source_no_longer_builds_creationflags_inline(self):
        """Pre-fix the spawn function built creationflags inline. The
        v1.10.6 refactor moved that logic into popen_detached. If a
        future contributor reintroduces the inline construction, this
        guard catches the divergence."""
        import inspect
        from claude_hooks.lsp_engine.client import _spawn_daemon
        src = inspect.getsource(_spawn_daemon)
        # The literal pattern that used to build creationflags inline.
        self.assertNotIn(
            'popen_kwargs["creationflags"] = (\n', src, (
                "_spawn_daemon is again building creationflags inline "
                "— it's bypassing popen_detached's BREAKAWAY semantics."
            ),
        )


if __name__ == "__main__":
    unittest.main()
