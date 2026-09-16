"""The test suite must not leave language servers behind.

Tests that exercise the MCP registry spawn a **real** lsp_engine daemon,
and tearing the registry down does not stop it. That is correct in
production — ``DaemonEngine.shutdown`` detaches only, because the daemon
is shared with the PostToolUse hook and every other session in the
project, so reaping one handle must not take their servers down. In a
test it is a leak: the daemon was spawned for a temp directory deleted
moments later, so nothing will attach to it again and nothing will stop
it.

Measured on solidpc 2026-09-16, before the guard: **156** live daemons
rooted at deleted temp directories, holding **3.37 GB** of RSS between
them and their language servers.

The guard has two halves and this covers both: ``stop_lsp_daemon_for``
(the explicit teardown a test calls) and ``_live_lsp_daemons`` (the
discovery the session-end safety net is built on). The safety net itself
is an autouse fixture, so its behaviour is asserted here against the
same primitives rather than by leaking a daemon to see what happens.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.client import connect_or_spawn  # noqa: E402
from claude_hooks.lsp_engine.daemon import pid_is_alive  # noqa: E402
from tests.conftest import (  # noqa: E402
    _live_lsp_daemons,
    stop_lsp_daemon_for,
)

POSIX_ONLY = unittest.skipUnless(
    Path("/proc").is_dir(),
    "daemon discovery reads /proc; the guard is a no-op elsewhere")


class StopHelperTests(unittest.TestCase):
    """``stop_lsp_daemon_for`` is what a test calls in its teardown."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        # Stop before the tree goes, or there is nothing to stop it with.
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: stop_lsp_daemon_for(self.root))
        (self.root / "cclsp.json").write_text(json.dumps({"servers": [
            {"extensions": ["py"], "command": ["true"], "rootDir": "."}]}),
            encoding="utf-8")

    def _spawn(self):
        return connect_or_spawn(self.root, session_id="leak-guard-test",
                                spawn_wait_s=15.0)

    @POSIX_ONLY
    def test_a_spawned_daemon_is_stopped(self) -> None:
        client = self._spawn()
        pid = client.status()["pid"]
        self.assertTrue(pid_is_alive(pid))
        client.close()

        self.assertTrue(stop_lsp_daemon_for(self.root))
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and pid_is_alive(pid):
            time.sleep(0.05)
        # `pid_is_alive` excludes zombies, which is load-bearing here:
        # this process spawned the daemon and never waits on it, so the
        # PID stays allocated after it exits. `os.kill(pid, 0)` alone
        # would report it running forever — and that is exactly what
        # `cleanup` used to see when deciding whether a state dir was
        # safe to remove.
        self.assertFalse(pid_is_alive(pid),
                         "daemon survived the teardown helper")

    @POSIX_ONLY
    def test_detaching_alone_leaves_it_running(self) -> None:
        """Why the helper exists at all.

        This is not a defect in ``shutdown`` — a shared daemon must
        survive one client letting go. It is why a *test* needs more
        than the registry teardown it already calls.
        """
        from claude_hooks.lsp_mcp.server import EngineRegistry

        reg = EngineRegistry()
        entry = reg.for_path(self.root / "a.py")
        pid = entry.engine._client.status()["pid"]
        reg.shutdown_all()
        self.assertTrue(pid_is_alive(pid),
                        "shutdown_all stopped a shared daemon")
        # ...which is exactly what the explicit teardown then handles.
        self.assertTrue(stop_lsp_daemon_for(self.root))

    def test_stopping_a_project_with_no_daemon_is_not_an_error(self) -> None:
        # The common case in teardown: most tests never spawned one.
        empty = Path(self.tmp.name).resolve() / "nothing-here"
        empty.mkdir()
        self.assertFalse(stop_lsp_daemon_for(empty))

    def test_a_nonexistent_path_is_not_an_error(self) -> None:
        self.assertFalse(stop_lsp_daemon_for(self.root / "gone" / "away"))


class ZombieTests(unittest.TestCase):
    """A stopped-but-unreaped daemon is not a running daemon.

    ``os.kill(pid, 0)`` succeeds on a zombie — the PID stays allocated
    so the parent can collect the exit status — but the daemon is gone:
    no memory, no language servers, no socket. Both callers of
    ``pid_is_alive`` want "is a daemon still serving", and for both the
    wrong answer is sticky: ``status`` reports a live daemon that
    answers nothing, and ``cleanup`` refuses to remove a state dir
    forever.

    It needs a spawner that outlives the daemon without reaping it.
    ``start_new_session=True`` detaches the session but does not
    reparent, so the MCP server — or a pytest run — stays the parent.
    """

    @POSIX_ONLY
    def test_a_zombie_is_not_alive(self) -> None:
        import subprocess
        # Started and left unreaped: Popen only collects the status when
        # wait() is called, so the PID stays in state Z until then.
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{zombie.pid}/stat").read_text(
                    encoding="ascii").rsplit(")", 1)[1].split()[0]
            except OSError:
                self.skipTest("process vanished before it could be seen")
            if state == "Z":
                break
            time.sleep(0.02)
        else:
            self.skipTest("could not observe the zombie state")

        # The old probe says yes...
        os.kill(zombie.pid, 0)          # does not raise
        # ...and the fixed one says no.
        self.assertFalse(pid_is_alive(zombie.pid))
        zombie.wait(timeout=10)

    @POSIX_ONLY
    def test_a_running_process_is_alive(self) -> None:
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertTrue(pid_is_alive(proc.pid))

    def test_a_pid_that_never_existed_is_not_alive(self) -> None:
        self.assertFalse(pid_is_alive(0))
        self.assertFalse(pid_is_alive(-1))


class DiscoveryTests(unittest.TestCase):
    """``_live_lsp_daemons`` is what the session-end net iterates."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: stop_lsp_daemon_for(self.root))

    @POSIX_ONLY
    def test_a_live_daemon_is_discovered_with_its_project(self) -> None:
        (self.root / "cclsp.json").write_text(json.dumps({"servers": [
            {"extensions": ["py"], "command": ["true"], "rootDir": "."}]}),
            encoding="utf-8")
        client = connect_or_spawn(self.root, session_id="leak-guard-test",
                                  spawn_wait_s=15.0)
        pid = client.status()["pid"]
        client.close()
        found = _live_lsp_daemons()
        self.assertIn(pid, found)
        self.assertEqual(os.path.realpath(found[pid]),
                         os.path.realpath(self.root))

    @POSIX_ONLY
    def test_it_reports_only_lsp_engine_daemons(self) -> None:
        # Every value must look like a project path, and every key like
        # a live pid. A looser match would have the session-end net
        # signalling unrelated processes.
        for pid, root in _live_lsp_daemons().items():
            self.assertIsInstance(pid, int)
            self.assertTrue(pid_is_alive(pid))
            self.assertTrue(str(root))

    def test_discovery_never_raises(self) -> None:
        # Runs on every session teardown, including Windows, where it is
        # a no-op. Returning nothing is fine; raising would fail runs
        # that had otherwise passed.
        self.assertIsInstance(_live_lsp_daemons(), dict)


class ScopeTests(unittest.TestCase):
    """The net stops what the session started, and nothing else."""

    def test_a_non_temp_project_is_reported_not_killed(self) -> None:
        # A developer's warm daemon for a real checkout must survive a
        # test run. The net prints and moves on rather than stopping it.
        import inspect
        from tests import conftest
        src = inspect.getsource(conftest._no_leaked_lsp_daemons)
        self.assertIn("tmp_root", src)
        self.assertIn("continue", src)
        # And pre-existing daemons are excluded by pid, not by path.
        self.assertIn("if pid in before", src)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
