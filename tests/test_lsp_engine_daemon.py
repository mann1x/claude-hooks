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
class TestPhase2PreloadAndGitWatch(unittest.TestCase):
    """Phase 2 acceptance: preload from code_graph + branch-switch
    bulk re-didOpen.
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.state = Path(self.tmp.name) / "state"

    def _seed_files(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            p = self.project / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    def _seed_graph(self, modules: list[tuple[str, str]],
                    imports: list[tuple[str, str]]) -> None:
        from claude_hooks.lsp_engine.preload import GRAPH_JSON_REL_PATH
        nodes = [
            {"id": mid, "type": "module", "file": file_rel}
            for mid, file_rel in modules
        ]
        edges = [
            {"source": s, "target": t, "type": "imports"}
            for s, t in imports
        ]
        out = self.project / GRAPH_JSON_REL_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"graph": {"nodes": nodes, "edges": edges}}),
            encoding="utf-8",
        )

    def _seed_git(self, branch: str = "main", sha: str = "a" * 40) -> None:
        git = self.project / ".git"
        (git / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (git / "HEAD").write_text(
            f"ref: refs/heads/{branch}\n", encoding="utf-8",
        )
        (git / "refs" / "heads" / branch).write_text(
            f"{sha}\n", encoding="utf-8",
        )

    def test_preload_opens_top_n_hot_files_on_start(self) -> None:
        self._seed_files({
            "pkg/utils.py": "U = 1\n",
            "pkg/api.py":   "A = 1\n",
            "pkg/cli.py":   "C = 1\n",
        })
        self._seed_graph(
            modules=[
                ("module:pkg.utils", "pkg/utils.py"),
                ("module:pkg.api",   "pkg/api.py"),
                ("module:pkg.cli",   "pkg/cli.py"),
            ],
            imports=[
                ("module:pkg.api",  "module:pkg.utils"),
                ("module:pkg.cli",  "module:pkg.utils"),
                ("module:pkg.cli",  "module:pkg.api"),
            ],
        )
        # Cap preload at 2 — utils + api should land, cli should not.
        from claude_hooks.lsp_engine.config import (
            EngineConfig,
            PreloadConfig,
        )
        cfg = EngineConfig(preload=PreloadConfig(max_files=2, use_code_graph=True))
        daemon = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            engine_config=cfg,
            state_base=self.state,
        )
        daemon.start()
        try:
            # Wait for the preload thread to drain. It runs in
            # background; poll the engine until the expected count
            # appears or we time out.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if len(daemon._engine.open_files()) >= 2:
                    break
                time.sleep(0.10)
            opened_paths = daemon._engine.open_files()
            self.assertEqual(len(opened_paths), 2)
            opened_names = {Path(u).name for u in opened_paths}
            self.assertEqual(opened_names, {"utils.py", "api.py"})
        finally:
            daemon.stop()

    def test_preload_disabled_when_use_code_graph_false(self) -> None:
        self._seed_files({"a.py": "x = 1\n"})
        self._seed_graph(
            modules=[("module:a", "a.py")],
            imports=[],
        )
        from claude_hooks.lsp_engine.config import (
            EngineConfig,
            PreloadConfig,
        )
        cfg = EngineConfig(preload=PreloadConfig(use_code_graph=False))
        daemon = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            engine_config=cfg,
            state_base=self.state,
        )
        daemon.start()
        try:
            time.sleep(0.30)
            self.assertEqual(daemon._engine.open_files(), [])
        finally:
            daemon.stop()

    def test_branch_switch_triggers_bulk_refresh(self) -> None:
        """Acceptance: after a branch switch, an open file's
        diagnostics reflect the file's *new on-disk content*. The
        fake LSP echoes ``len(content)`` so we can verify the new
        content reached the LSP child.
        """
        self._seed_files({"x.py": "short"})
        self._seed_git(branch="main")
        daemon = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            state_base=self.state,
            git_poll_interval=0.05,
        )
        daemon.start()
        try:
            sock = socket_path_for(self.project, base=self.state)
            with LspEngineClient(sock, "session-A") as c:
                target = self.project / "x.py"
                c.did_open(target, "short")
                diags, _ = c.diagnostics(target, diag_timeout_s=2.0)
                self.assertEqual(diags[0]["message"], "len=5")

                # Synthesize branch switch by mutating HEAD + having
                # the on-disk content also change (as a real branch
                # switch would).
                target.write_text("longer-disk-content", encoding="utf-8")
                # Add the second branch ref so HEAD switch resolves.
                (self.project / ".git" / "refs" / "heads" / "feature").write_text(
                    "b" * 40 + "\n", encoding="utf-8",
                )
                (self.project / ".git" / "HEAD").write_text(
                    "ref: refs/heads/feature\n", encoding="utf-8",
                )

                # Wait for the watcher (poll=0.05s) to fire and
                # refresh_open_files to send the new content.
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    diags, _ = c.diagnostics(target, diag_timeout_s=1.0)
                    if diags and diags[0]["message"] == "len=19":
                        break
                    time.sleep(0.10)
                self.assertEqual(diags[0]["message"], "len=19")
        finally:
            daemon.stop()

    def test_non_git_project_starts_cleanly_with_inactive_watcher(self) -> None:
        # No .git/ directory — daemon should start, watcher should
        # silently sit out.
        self._seed_files({"x.py": "x = 1"})
        daemon = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            state_base=self.state,
            git_poll_interval=0.05,
        )
        daemon.start()
        try:
            self.assertFalse(daemon._git_watcher.is_git_repo)
            time.sleep(0.20)  # let any over-eager watcher fire if it wanted to
        finally:
            daemon.stop()


@unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
class TestPhase3CompileAware(unittest.TestCase):
    """Compile-aware mode merges compile-side diagnostics into the
    same ``diagnostics`` op response the LSPs feed. Verified end to
    end with a fake compile command that emits text-format output."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.state = Path(self.tmp.name) / "state"

    def _fake_compile_command(self, file_rel: str, message: str) -> tuple:
        """Build a python -c command that emits one text-format
        diagnostic for ``file_rel`` against the project root."""
        return (
            sys.executable, "-c",
            f"print({file_rel!r} + ':1:1: error: ' + {message!r})",
        )

    def test_disabled_by_default(self) -> None:
        """Sanity: with compile_aware off (the default), the daemon
        starts cleanly and never spawns compile processes even with
        a non-empty commands map."""
        from claude_hooks.lsp_engine.config import (
            CompileAwareConfig,
            EngineConfig,
        )
        cfg = EngineConfig(
            compile_aware=CompileAwareConfig(
                enabled=False,
                commands={"py": ("python", "-c", "print('should not run')")},
            ),
        )
        daemon = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            engine_config=cfg,
            state_base=self.state,
        )
        daemon.start()
        try:
            self.assertIsNone(daemon._compile)
        finally:
            daemon.stop()

    def test_enabled_but_empty_commands_no_orchestrator(self) -> None:
        """Edge case: enabled=true but empty commands dict — daemon
        should NOT construct an orchestrator (avoids confusing
        half-active state)."""
        from claude_hooks.lsp_engine.config import (
            CompileAwareConfig,
            EngineConfig,
        )
        cfg = EngineConfig(
            compile_aware=CompileAwareConfig(enabled=True, commands={}),
        )
        daemon = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            engine_config=cfg,
            state_base=self.state,
        )
        daemon.start()
        try:
            self.assertIsNone(daemon._compile)
        finally:
            daemon.stop()

    def test_compile_diagnostics_merged_with_lsp_diagnostics(self) -> None:
        """The acceptance criterion. did_open a Python file → fake
        LSP emits len=N diagnostic; fake compile emits an "oh no"
        diagnostic. The diagnostics op response includes both, with
        distinct ``source`` fields.
        """
        from claude_hooks.lsp_engine.config import (
            CompileAwareConfig,
            EngineConfig,
        )
        target_rel = "x.py"
        target_abs = self.project / target_rel
        target_abs.write_text("hello\n", encoding="utf-8")

        cfg = EngineConfig(
            compile_aware=CompileAwareConfig(
                enabled=True,
                commands={
                    "py": self._fake_compile_command(
                        str(target_abs), "compile-side issue",
                    ),
                },
            ),
        )
        daemon = Daemon(
            project_root=self.project,
            servers=[_fake_spec()],
            engine_config=cfg,
            state_base=self.state,
        )
        daemon.start()
        try:
            sock = socket_path_for(self.project, base=self.state)
            with LspEngineClient(sock, "session-A") as c:
                c.did_open(target_abs, "hello")
                # Force a compile run immediately rather than waiting
                # the default 1.5s debounce.
                daemon._compile.force_run_all()
                # Poll until the compile diagnostic shows up.
                deadline = time.monotonic() + 5.0
                merged: list[dict] = []
                while time.monotonic() < deadline:
                    diags, _ = c.diagnostics(target_abs, diag_timeout_s=2.0)
                    sources = {d["source"] for d in diags}
                    if "fake-lsp" in sources and "python" in sources:
                        merged = diags
                        break
                    time.sleep(0.10)
                self.assertTrue(merged, "compile + LSP diagnostics did not merge")
                lsp_d = next(d for d in merged if d["source"] == "fake-lsp")
                cmp_d = next(d for d in merged if d["source"] == "python")
                self.assertEqual(lsp_d["message"], "len=5")
                self.assertEqual(cmp_d["message"], "compile-side issue")
        finally:
            daemon.stop()


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
