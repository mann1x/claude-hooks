"""End-to-end tests for the LSP engine daemon.

The Phase 1 acceptance criteria from ``docs/PLAN-lsp-engine.md``:

> two sessions editing different files run concurrently with no
> contention; two sessions editing the same file serialize via the
> affinity lock with the second session's didChange queued until
> lock releases.

These tests assemble the full stack — Daemon + Engine + lock manager
+ IPC + LspEngineClient — using the fake LSP server fixture.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_hooks.lsp_engine.config import EngineConfig, LspServerSpec, SessionLockConfig
from claude_hooks.lsp_engine.daemon import (
    Daemon,
    DaemonAlreadyRunning,
    project_dir,
    socket_path_for,
)
from claude_hooks.lsp_engine.client import (
    LspEngineClient,
    connect_or_spawn,
    daemon_pid,
)

_FAKE_SERVER = Path(__file__).parent / "lsp_engine_fake_server.py"


def _fake_spec() -> LspServerSpec:
    return LspServerSpec(
        extensions=("py", "fake"),
        command=(sys.executable, str(_FAKE_SERVER)),
        root_dir=".",
    )


def _short_lock_config() -> EngineConfig:
    """Override the 30-second debounce so locks expire fast enough
    for tests to verify drain behaviour without hanging."""
    return EngineConfig(
        session_locks=SessionLockConfig(
            debounce_seconds=0.5,
            query_timeout_ms=200,
        ),
    )


@unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
class TestDaemonLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.daemon = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            state_base=self.state,
        )

    def tearDown(self) -> None:
        self.daemon.stop()

    def test_start_creates_socket_and_lock_file(self) -> None:
        self.daemon.start()
        sock = socket_path_for(self.project, base=self.state)
        self.assertTrue(sock.exists())
        lock = project_dir(self.project, base=self.state) / "daemon.lock"
        self.assertTrue(lock.exists())
        # Lock file contains the daemon's PID on the first line.
        pid_line = lock.read_text(encoding="ascii").splitlines()[0]
        self.assertEqual(int(pid_line), os.getpid())

    def test_second_start_for_same_project_raises_already_running(self) -> None:
        self.daemon.start()
        other = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            state_base=self.state,
        )
        with self.assertRaises(DaemonAlreadyRunning):
            other.start()

    def test_stop_cleans_socket_and_lock(self) -> None:
        self.daemon.start()
        sock = socket_path_for(self.project, base=self.state)
        lock = project_dir(self.project, base=self.state) / "daemon.lock"
        self.assertTrue(sock.exists())
        self.daemon.stop()
        self.assertFalse(sock.exists())
        self.assertFalse(lock.exists())


@unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
class TestDaemonClientFlow(unittest.TestCase):
    """Single-client happy path: open, change, diagnostics, close,
    detach. The fake LSP echoes len(content), so diagnostics let us
    verify the latest content actually reached the LSP child."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.daemon = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            state_base=self.state,
        )
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.sock = socket_path_for(self.project, base=self.state)

    def test_attach_did_open_diagnostics_round_trip(self) -> None:
        with LspEngineClient(self.sock, "session-A") as c:
            target = self.project / "x.py"
            target.write_text("hi", encoding="utf-8")
            opened = c.did_open(target, "hi")
            self.assertTrue(opened)
            diags, stale = c.diagnostics(target, lock_timeout_ms=200, diag_timeout_s=2.0)
            self.assertFalse(stale)
            self.assertEqual(len(diags), 1)
            self.assertEqual(diags[0]["message"], "len=2")

    def test_did_change_updates_diagnostics(self) -> None:
        with LspEngineClient(self.sock, "session-A") as c:
            target = self.project / "x.py"
            c.did_open(target, "hi")
            forwarded, queued = c.did_change(target, "longer content")
            self.assertTrue(forwarded)
            self.assertIsNone(queued)
            diags, _ = c.diagnostics(target, diag_timeout_s=2.0)
            self.assertEqual(diags[0]["message"], "len=14")

    def test_status_reports_session_and_open_files(self) -> None:
        with LspEngineClient(self.sock, "session-A") as c:
            target = self.project / "x.py"
            c.did_open(target, "hi")
            status = c.status()
            self.assertIn("session-A", status["sessions"])
            self.assertEqual(len(status["open_files"]), 1)


@unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
class TestSessionAffinityE2E(unittest.TestCase):
    """The Phase 1 acceptance criteria for Decision 5.

    Uses a 0.5-second debounce so a queued change can drain inside
    the test without us waiting half a minute.
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.daemon = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            engine_config=_short_lock_config(),
            state_base=self.state,
        )
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.sock = socket_path_for(self.project, base=self.state)

    def test_two_sessions_different_files_run_concurrently(self) -> None:
        with LspEngineClient(self.sock, "A") as ca, \
             LspEngineClient(self.sock, "B") as cb:
            file_a = self.project / "alpha.py"
            file_b = self.project / "beta.py"
            opened_a = ca.did_open(file_a, "alpha")
            opened_b = cb.did_open(file_b, "beta-content")
            self.assertTrue(opened_a)
            self.assertTrue(opened_b)
            forwarded_a, queued_a = ca.did_change(file_a, "alpha-extra")
            forwarded_b, queued_b = cb.did_change(file_b, "beta-extra")
            self.assertTrue(forwarded_a)
            self.assertTrue(forwarded_b)
            self.assertIsNone(queued_a)
            self.assertIsNone(queued_b)
            # Both files have their own diagnostics — no cross-talk.
            diags_a, _ = ca.diagnostics(file_a, diag_timeout_s=2.0)
            diags_b, _ = cb.diagnostics(file_b, diag_timeout_s=2.0)
            self.assertEqual(diags_a[0]["message"], "len=11")  # "alpha-extra"
            self.assertEqual(diags_b[0]["message"], "len=10")  # "beta-extra"

    def test_two_sessions_same_file_serialize_via_lock(self) -> None:
        with LspEngineClient(self.sock, "A") as ca, \
             LspEngineClient(self.sock, "B") as cb:
            target = self.project / "shared.py"
            ca.did_open(target, "first")
            # B's didChange while A holds the lock → queued.
            forwarded_b, queued_behind = cb.did_change(target, "second")
            self.assertFalse(forwarded_b)
            self.assertEqual(queued_behind, "A")
            # Diagnostics for A still reflect "first" (5).
            diags_a, _ = ca.diagnostics(target, diag_timeout_s=2.0)
            self.assertEqual(diags_a[0]["message"], "len=5")

    def test_lock_drains_to_queued_session_after_debounce(self) -> None:
        with LspEngineClient(self.sock, "A") as ca, \
             LspEngineClient(self.sock, "B") as cb:
            target = self.project / "shared.py"
            ca.did_open(target, "first")
            cb.did_change(target, "second-edit")  # queued behind A
            # Wait past A's debounce + sweeper interval.
            time.sleep(2.0)
            # B's queued change should now be applied to the LSP, so
            # diagnostics from either session reflect "second-edit".
            diags, _ = cb.diagnostics(target, diag_timeout_s=2.0)
            self.assertEqual(diags[0]["message"], "len=11")  # len("second-edit")

    def test_detach_releases_lock_and_drains_queue(self) -> None:
        with LspEngineClient(self.sock, "A") as ca, \
             LspEngineClient(self.sock, "B") as cb:
            target = self.project / "shared.py"
            ca.did_open(target, "first")
            cb.did_change(target, "B-content")  # queued
            # Detach A immediately → queue drains, B becomes owner,
            # B's content reaches the LSP without waiting for debounce.
            ca.detach()
            # Give the daemon a tick to apply the drained change.
            time.sleep(0.30)
            diags, _ = cb.diagnostics(target, diag_timeout_s=2.0)
            self.assertEqual(diags[0]["message"], "len=9")  # "B-content"


@unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
class TestConnectOrSpawn(unittest.TestCase):
    """The hook entry point — spawn the daemon if it isn't running,
    return a connected client. Uses a real subprocess for the spawn.
    """

    def test_spawn_when_no_daemon_running(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            state = Path(tmp) / "state"
            # Provide a cclsp.json so the spawned daemon has servers.
            (project / "cclsp.json").write_text(
                json.dumps({
                    "servers": [{
                        "extensions": ["py", "fake"],
                        "command": [sys.executable, str(_FAKE_SERVER)],
                        "rootDir": ".",
                    }],
                }),
                encoding="utf-8",
            )
            self.assertIsNone(daemon_pid(project, state_base=state))
            client = connect_or_spawn(
                project, "spawn-test",
                state_base=state, spawn_wait_s=10.0,
            )
            try:
                pid = daemon_pid(project, state_base=state)
                self.assertIsNotNone(pid)
                self.assertNotEqual(pid, os.getpid())  # detached process
                # Smoke: the spawned daemon answers status.
                status = client.status()
                self.assertEqual(status["project"], str(project.resolve()))
            finally:
                # Send shutdown via the client so the spawned process exits.
                try:
                    client._call("shutdown")
                except Exception:
                    pass
                client.close()
                # Daemon needs a moment to actually stop after the
                # in-thread shutdown runs.
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if daemon_pid(project, state_base=state) is None:
                        break
                    time.sleep(0.10)


if __name__ == "__main__":
    unittest.main()
