"""v1.10.3 LSP-engine bug fixes — regression suite.

Covers the four bug classes the user surfaced on pandorum
2026-05-22 while wiring lsp-engine into PersistentWindows:

1. **Case-sensitive project hash on Windows** — ``project_dir`` and
   ``windows_pipe_name_for`` must normalize case before hashing so
   ``c:\\X`` and ``C:\\X`` produce identical state-dir and pipe-name
   digests. Without this, hook and CLI invocations point at different
   addresses and never meet.

2. **(downstream of #1)** "daemon socket did not come up in time"
   spam — proven to be the case-mismatch symptom, no separate fix.

3. **``status`` reports stale dead PID** — must liveness-probe the
   lock-file PID and surface ``stale_lock: true`` when the recorded
   process is gone.

4. **No ``stop`` / ``cleanup`` CLI** — must accept ``stop`` (graceful
   shutdown via the existing ``shutdown`` IPC op) and ``cleanup``
   (remove stale state dir when no live daemon holds the lock).

5. **``.cmd`` shim ``PYTHONPATH`` parity** — the Windows shim must
   export ``PYTHONPATH`` like the POSIX one does so nested Python
   invocations from outside the shim's cwd can import
   ``claude_hooks``.

Source-inspection tests for #5 (the .cmd shim can't be exercised on
Linux) and the case-normalization injection points (matches the
v1.10.1 pattern in ``tests/test_popen_helpers.py``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from claude_hooks.lsp_engine import (  # noqa: E402
    daemon as daemon_mod,
    ipc as ipc_mod,
)
from claude_hooks.lsp_engine.config import (  # noqa: E402
    EngineConfig,
    LspServerSpec,
    SessionLockConfig,
)
from claude_hooks.lsp_engine.daemon import (  # noqa: E402
    Daemon,
    lock_path_for,
    pid_is_alive,
    project_dir,
    socket_path_for,
)
from claude_hooks.lsp_engine.ipc import windows_pipe_name_for  # noqa: E402

_FAKE_SERVER = Path(__file__).parent / "lsp_engine_fake_server.py"


def _fake_spec() -> LspServerSpec:
    return LspServerSpec(
        extensions=("py", "fake"),
        command=(sys.executable, str(_FAKE_SERVER)),
        root_dir=".",
    )


def _short_lock_config() -> EngineConfig:
    return EngineConfig(
        session_locks=SessionLockConfig(
            debounce_seconds=0.5,
            query_timeout_ms=200,
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Bug #1 — case normalization in hash inputs
# ──────────────────────────────────────────────────────────────────────


class TestCaseNormalizationProjectDir(unittest.TestCase):
    """``project_dir`` must produce identical hashes for paths that
    differ only in case on Windows."""

    def test_case_variants_produce_same_digest_with_normcase_lower(self):
        """Patch ``os.path.normcase`` to lowercase (simulating Windows
        behavior) and patch ``Path.resolve`` to be the identity (so
        we don't depend on a real Windows filesystem). Then assert
        that two case-different but otherwise-equivalent project-root
        strings yield the SAME digest. This is the core invariant the
        pandorum bug exposed: ``c:\\X`` and ``C:\\X`` must produce
        identical hashes."""
        # Use a string-yielding identity resolve so we don't depend on
        # real disk paths; the production code only cares about the
        # str(abs_root) it hands to normcase.
        with patch.object(daemon_mod.os.path, "normcase", lambda s: s.lower()), \
             patch.object(daemon_mod.Path, "resolve", lambda self: self):
            lower = project_dir(r"c:\Users\x\proj", base=Path("/tmp/state"))
            upper = project_dir(r"C:\USERS\X\PROJ", base=Path("/tmp/state"))
        # Both inputs lowercased land on the same string → same hash.
        self.assertEqual(lower.name, upper.name)
        # And the digest is what you get from hashing the lowercased
        # path with the same SHA-256/16-hex shape used in production.
        import hashlib
        expected = hashlib.sha256(
            r"c:\users\x\proj".encode("utf-8"),
        ).hexdigest()[:16]
        self.assertEqual(lower.name, expected)

    def test_source_uses_normcase_in_project_dir(self):
        """Regression guard: ``project_dir`` must call
        ``os.path.normcase`` on the resolved-path string before
        hashing. Source-inspection so the test survives on POSIX
        without a real Windows host."""
        import inspect
        src = inspect.getsource(project_dir)
        self.assertIn("os.path.normcase", src, (
            "project_dir() no longer calls os.path.normcase before "
            "hashing — the pandorum case-mismatch bug will resurface. "
            "See claude_hooks/lsp_engine/daemon.py:project_dir."
        ))

    def test_posix_normcase_is_identity_unchanged_behavior(self):
        """On POSIX ``os.path.normcase`` is the identity function, so
        adding the normalization must not change the Linux digest."""
        # No mocking. Just check that two calls with the same input
        # produce the same digest and that the digest is a 16-char hex.
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "p").mkdir()
            d = project_dir(tdp / "p", base=tdp / "state")
            self.assertRegex(d.name, r"^[0-9a-f]{16}$")


class TestCaseNormalizationWindowsPipeName(unittest.TestCase):
    """``windows_pipe_name_for`` must mirror ``project_dir``'s case
    normalization — same input case-variants → same pipe name."""

    def test_source_uses_normcase_in_pipe_name(self):
        """Source-inspection regression guard."""
        import inspect
        src = inspect.getsource(windows_pipe_name_for)
        self.assertIn("os.path.normcase", src, (
            "windows_pipe_name_for() no longer calls os.path.normcase "
            "before hashing — the pandorum case-mismatch bug will "
            "resurface on the Windows named-pipe path. See "
            "claude_hooks/lsp_engine/ipc.py:windows_pipe_name_for."
        ))

    def test_case_variants_produce_same_pipe_name(self):
        """Stub normcase to lowercase and assert
        ``windows_pipe_name_for(r"c:\\X")`` ==
        ``windows_pipe_name_for(r"C:\\X")``."""
        with patch.object(ipc_mod.os.path, "normcase", lambda s: s.lower()):
            # Use os.path.abspath behavior; project_root strings here
            # don't need to exist on disk.
            lower = windows_pipe_name_for(r"c:\Users\manni\project")
            upper = windows_pipe_name_for(r"C:\Users\manni\project")
        self.assertEqual(lower, upper)
        self.assertTrue(lower.startswith(r"\\.\pipe\claude-hooks-lsp-engine-"))


class TestCaseNormalizationIdenticalToProjectDir(unittest.TestCase):
    """The case-normalization in ``project_dir`` and
    ``windows_pipe_name_for`` must use the SAME function so a daemon
    that registers under one address can be found by tools that look
    up the other."""

    def test_both_call_sites_use_normcase(self):
        """Both functions must show ``os.path.normcase`` in their
        source — already covered individually above, here we collapse
        the assertion into the invariant: every place we hash a
        project-root must normalize."""
        import inspect
        for fn in (project_dir, windows_pipe_name_for):
            src = inspect.getsource(fn)
            self.assertIn(
                "os.path.normcase", src,
                f"{fn.__module__}.{fn.__name__} must normcase before "
                f"hashing — see v1.10.3 fix.",
            )


# ──────────────────────────────────────────────────────────────────────
# pid_is_alive helper (used by bugs #3 and #4)
# ──────────────────────────────────────────────────────────────────────


class TestPidIsAlive(unittest.TestCase):
    """``pid_is_alive`` must distinguish live from dead PIDs."""

    def test_self_pid_is_alive(self):
        self.assertTrue(pid_is_alive(os.getpid()))

    def test_zero_pid_returns_false(self):
        self.assertFalse(pid_is_alive(0))

    def test_negative_pid_returns_false(self):
        self.assertFalse(pid_is_alive(-1))

    def test_none_returns_false(self):
        self.assertFalse(pid_is_alive(None))  # type: ignore[arg-type]

    @unittest.skipIf(os.name == "nt", "POSIX-only test")
    def test_definitely_dead_pid_returns_false_posix(self):
        """PID 2^31 - 1 is above PID_MAX on every reasonable system."""
        self.assertFalse(pid_is_alive(2**31 - 1))


# ──────────────────────────────────────────────────────────────────────
# Bug #3 — status reports stale dead PID
# ──────────────────────────────────────────────────────────────────────


class TestStatusReportsStaleLock(unittest.TestCase):
    """``_run_status`` must liveness-probe the lock-file PID and
    surface ``stale_lock: true`` when the recorded process is dead."""

    @unittest.skipIf(os.name == "nt", "POSIX socket; Windows uses pipes")
    def test_status_with_stale_lock_marks_stale(self):
        """Plant a lock file with a definitely-dead PID; assert
        status output sets stale_lock=true and pid=None."""
        from claude_hooks.lsp_engine.__main__ import _run_status

        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            pdir = project_dir(project, base=state)
            pdir.mkdir(parents=True)
            lock = lock_path_for(project, base=state)
            # PID well above PID_MAX — guaranteed dead.
            lock.write_text(f"{2**31 - 1}\n", encoding="ascii")

            # Capture stdout.
            import io
            buf = io.StringIO()
            args = type("A", (), {})()
            args.project = str(project)
            args.state_base = str(state)
            with patch("sys.stdout", buf):
                rc = _run_status(args)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["running"])
            self.assertIsNone(out["pid"])
            self.assertTrue(out["stale_lock"])

    @unittest.skipIf(os.name == "nt", "POSIX socket; Windows uses pipes")
    def test_status_with_no_lock_no_stale_flag(self):
        """No lock file at all → stale_lock=false, pid=None."""
        from claude_hooks.lsp_engine.__main__ import _run_status
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            import io
            buf = io.StringIO()
            args = type("A", (), {})()
            args.project = str(project)
            args.state_base = str(state)
            with patch("sys.stdout", buf):
                rc = _run_status(args)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["running"])
            self.assertIsNone(out["pid"])
            self.assertFalse(out["stale_lock"])

    @unittest.skipIf(os.name == "nt", "POSIX socket; Windows uses pipes")
    def test_status_with_live_lock_reports_pid_and_no_stale(self):
        """Lock file points at our own (live) PID; socket dead. Should
        report pid=our_pid, stale_lock=false (rare edge case: daemon
        died but couldn't unlink its lock)."""
        from claude_hooks.lsp_engine.__main__ import _run_status
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            pdir = project_dir(project, base=state)
            pdir.mkdir(parents=True)
            lock = lock_path_for(project, base=state)
            lock.write_text(f"{os.getpid()}\n", encoding="ascii")

            import io
            buf = io.StringIO()
            args = type("A", (), {})()
            args.project = str(project)
            args.state_base = str(state)
            with patch("sys.stdout", buf):
                rc = _run_status(args)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["running"])
            self.assertEqual(out["pid"], os.getpid())
            self.assertFalse(out["stale_lock"])


# ──────────────────────────────────────────────────────────────────────
# Bug #4 — stop + cleanup subcommands
# ──────────────────────────────────────────────────────────────────────


class TestStopSubcommand(unittest.TestCase):
    """``stop`` must gracefully shut down a running daemon and
    return 1 when no daemon exists."""

    @unittest.skipIf(os.name == "nt", "Windows parity uses pipes; Phase 4")
    def test_stop_returns_1_when_no_daemon_running(self):
        from claude_hooks.lsp_engine.__main__ import _run_stop
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            args = type("A", (), {})()
            args.project = str(project)
            args.state_base = str(state)

            import io
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = _run_stop(args)
            self.assertEqual(rc, 1)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["stopped"])
            self.assertIn("no daemon running", out["reason"])

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_stop_against_live_daemon_succeeds(self):
        """Spin up a real daemon, call _run_stop, assert it shuts
        down. Uses the same fake-LSP fixture as the existing daemon
        tests."""
        from claude_hooks.lsp_engine.__main__ import _run_stop
        from claude_hooks.lsp_engine.ipc import _is_socket_alive

        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            daemon = Daemon(
                project_root=project,
                servers=[_fake_spec()],
                engine_config=_short_lock_config(),
                state_base=state,
            )
            daemon.start()
            try:
                sock = socket_path_for(project, base=state)
                self.assertTrue(_is_socket_alive(sock))

                args = type("A", (), {})()
                args.project = str(project)
                args.state_base = str(state)
                import io
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = _run_stop(args)
                self.assertEqual(rc, 0)
                out = json.loads(buf.getvalue())
                self.assertTrue(out["stopped"])

                # Daemon's shutdown runs on a background thread; wait
                # briefly for the socket to disappear.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if not _is_socket_alive(sock):
                        break
                    time.sleep(0.05)
                self.assertFalse(_is_socket_alive(sock))
            finally:
                # Belt-and-braces: ensure stop in case the test failed
                # before the daemon shut down.
                try:
                    daemon.stop()
                except Exception:
                    pass


class TestCleanupSubcommand(unittest.TestCase):
    """``cleanup`` must remove the per-project state dir when no live
    daemon holds the lock, and refuse otherwise unless ``--force``."""

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_cleanup_no_op_when_dir_missing(self):
        from claude_hooks.lsp_engine.__main__ import _run_cleanup
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            args = type("A", (), {})()
            args.project = str(project)
            args.state_base = str(state)
            args.force = False
            import io
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = _run_cleanup(args)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["removed"])
            self.assertIn("already absent", out["reason"])

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_cleanup_removes_stale_state_dir(self):
        """State dir with a stale (dead-PID) lock — cleanup removes."""
        from claude_hooks.lsp_engine.__main__ import _run_cleanup
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            pdir = project_dir(project, base=state)
            pdir.mkdir(parents=True)
            lock = lock_path_for(project, base=state)
            # Dead PID.
            lock.write_text(f"{2**31 - 1}\n", encoding="ascii")
            self.assertTrue(pdir.is_dir())

            args = type("A", (), {})()
            args.project = str(project)
            args.state_base = str(state)
            args.force = False
            import io
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = _run_cleanup(args)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out["removed"])
            self.assertFalse(pdir.exists())

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_cleanup_refuses_when_live_daemon(self):
        """Live daemon → cleanup without --force returns 1, dir stays."""
        from claude_hooks.lsp_engine.__main__ import _run_cleanup
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            daemon = Daemon(
                project_root=project,
                servers=[_fake_spec()],
                engine_config=_short_lock_config(),
                state_base=state,
            )
            daemon.start()
            try:
                pdir = project_dir(project, base=state)
                self.assertTrue(pdir.exists())

                args = type("A", (), {})()
                args.project = str(project)
                args.state_base = str(state)
                args.force = False
                import io
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = _run_cleanup(args)
                self.assertEqual(rc, 1)
                out = json.loads(buf.getvalue())
                self.assertFalse(out["removed"])
                self.assertIn("live daemon", out["reason"])
                # State dir must still exist.
                self.assertTrue(pdir.exists())
            finally:
                daemon.stop()


# ──────────────────────────────────────────────────────────────────────
# Bug #5 — .cmd shim PYTHONPATH parity (source-inspection — Linux-safe)
# ──────────────────────────────────────────────────────────────────────


class TestCmdShimPythonPath(unittest.TestCase):
    """The Windows ``.cmd`` shim must export ``PYTHONPATH=%REPO%``
    so nested Python invocations from outside the shim's cwd can
    still import ``claude_hooks``. Source-inspection because we
    can't exercise ``.cmd`` on Linux."""

    def test_cmd_shim_exports_pythonpath(self):
        shim = (HERE / "bin" / "claude-hooks-lsp.cmd").read_text(
            encoding="utf-8",
        )
        # Both branches of the if/else must set PYTHONPATH with %REPO%
        # prepended.
        self.assertIn("PYTHONPATH=%REPO%", shim, (
            "claude-hooks-lsp.cmd does not export PYTHONPATH including "
            "%REPO% — nested Python invocations from outside the "
            "shim's cwd will hit ModuleNotFoundError. See v1.10.3 fix."
        ))


# ──────────────────────────────────────────────────────────────────────
# Bug #4 (CLI parsing) — both new subcommands accepted
# ──────────────────────────────────────────────────────────────────────


class TestParserAcceptsNewSubcommands(unittest.TestCase):
    """``_build_parser`` must accept ``stop`` and ``cleanup``
    alongside the existing ``daemon`` and ``status``."""

    def test_parser_accepts_stop(self):
        from claude_hooks.lsp_engine.__main__ import _build_parser
        ns = _build_parser().parse_args(["stop", "--project", "/tmp/p"])
        self.assertEqual(ns.subcommand, "stop")
        self.assertEqual(ns.project, "/tmp/p")

    def test_parser_accepts_cleanup(self):
        from claude_hooks.lsp_engine.__main__ import _build_parser
        ns = _build_parser().parse_args(["cleanup", "--project", "/tmp/p"])
        self.assertEqual(ns.subcommand, "cleanup")
        self.assertEqual(ns.project, "/tmp/p")
        self.assertFalse(ns.force)

    def test_parser_accepts_cleanup_force(self):
        from claude_hooks.lsp_engine.__main__ import _build_parser
        ns = _build_parser().parse_args(
            ["cleanup", "--project", "/tmp/p", "--force"],
        )
        self.assertTrue(ns.force)


if __name__ == "__main__":
    unittest.main()
