"""Tests for the #222 install.py robustness helpers (2026-05-19).

The pandorum v1.8.1 deploy surfaced five systemic issues:
  - daemon restart race (End → Run before TIME_WAIT clears)
  - opposite-service-mode task left registered after a mode flip
  - duplicate consultants engines on a host that previously ran the
    other mode
  - stale ``__pycache__`` keeping the daemon on pre-pull bytecode
  - false-negative "not responding within 20s" message from a
    too-short health-check window

#222 adds six helpers to address them. Tests here pin the helpers'
contracts so the next regression is caught before it ships.

The helpers are platform-conditional but the public surfaces are
uniform; each test stubs ``install.os.name`` / ``install.subprocess``
so the assertions run on the build host regardless of OS.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(scope="module")
def install_mod():
    """Load install.py as a module so we can poke its internals
    without invoking ``main()``."""
    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "install", repo_root / "install.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ----------------------- _wait_for_port_free ------------------- #

class TestWaitForPortFree:
    def test_returns_true_when_port_already_free(self, install_mod):
        # Random high port — nothing should be listening.
        free_port = self._unused_port()
        assert install_mod._wait_for_port_free(
            free_port, timeout=0.5) is True

    def test_returns_false_when_port_stays_busy(self, install_mod):
        # A backlog of 1 + the helper's repeated connect attempts fill
        # the accept queue and subsequent connects get refused. Use a
        # busy-accept thread so connect always succeeds during the
        # whole timeout window — that's the real "port stays busy"
        # condition the helper guards against.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(32)
        stop = threading.Event()

        def _drain():
            while not stop.is_set():
                s.settimeout(0.2)
                try:
                    conn, _ = s.accept()
                    conn.close()
                except socket.timeout:
                    continue
                except OSError:
                    break

        accept_thread = threading.Thread(target=_drain, daemon=True)
        accept_thread.start()
        try:
            t0 = time.monotonic()
            assert install_mod._wait_for_port_free(
                port, timeout=1.0) is False
            # Sanity: roughly waited the timeout (not way longer).
            assert time.monotonic() - t0 < 3.0
        finally:
            stop.set()
            s.close()
            accept_thread.join(timeout=1.0)

    def test_returns_true_when_port_becomes_free_mid_wait(self, install_mod):
        # Bind + busy-accept, then release in a worker thread after
        # 200 ms so the helper observes free port mid-wait.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(32)
        stop = threading.Event()

        def _drain():
            while not stop.is_set():
                s.settimeout(0.1)
                try:
                    conn, _ = s.accept()
                    conn.close()
                except socket.timeout:
                    continue
                except OSError:
                    break

        accept_thread = threading.Thread(target=_drain, daemon=True)
        accept_thread.start()

        def _release():
            time.sleep(0.2)
            stop.set()
            s.close()

        threading.Thread(target=_release, daemon=True).start()
        assert install_mod._wait_for_port_free(port, timeout=2.0) is True
        accept_thread.join(timeout=1.0)

    @staticmethod
    def _unused_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port


# ----------------------- _find_claude_hooks_pythonw_processes -- #

class TestFindClaudeHooksProcesses:
    def test_posix_returns_empty(self, install_mod):
        with patch.object(install_mod.os, "name", "posix"):
            assert install_mod._find_claude_hooks_pythonw_processes() == []

    def test_windows_parses_powershell_json(self, install_mod):
        fake_stdout = json.dumps([
            {"ProcessId": 1111,
             "CommandLine": (
                 "C:\\Users\\u\\miniconda3\\envs\\claude-hooks"
                 "\\pythonw.exe \"C:\\Users\\u\\claude-hooks"
                 "\\run_daemon.py\""
             )},
            {"ProcessId": 2222,
             "CommandLine": (
                 "C:\\Users\\u\\miniconda3\\envs\\claude-hooks"
                 "\\pythonw.exe -m claude_hooks.consultants_forwarder"
             )},
            {"ProcessId": 3333,
             "CommandLine": (
                 "C:\\Users\\u\\miniconda3\\envs"
                 "\\claude-hooks-consultants\\pythonw.exe "
                 "-m consultants.server --host 127.0.0.1 --port 38095"
             )},
            {"ProcessId": 4444,
             "CommandLine": "C:\\Python311\\pythonw.exe other.py"},  # noise
        ])
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod.subprocess, "run") as run:
            run.return_value = type("R", (), {
                "returncode": 0, "stdout": fake_stdout, "stderr": ""})()
            procs = install_mod._find_claude_hooks_pythonw_processes()
        pids = sorted(p for p, _ in procs)
        assert pids == [1111, 2222, 3333]  # 4444 filtered out

    def test_windows_handles_single_object_response(self, install_mod):
        # ConvertTo-Json emits a single object when only one row, not
        # a list. The helper must handle both shapes.
        fake_stdout = json.dumps(
            {"ProcessId": 5555,
             "CommandLine": "pythonw.exe run_daemon.py"}
        )
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod.subprocess, "run") as run:
            run.return_value = type("R", (), {
                "returncode": 0, "stdout": fake_stdout, "stderr": ""})()
            procs = install_mod._find_claude_hooks_pythonw_processes()
        assert procs == [(5555, "pythonw.exe run_daemon.py")]

    def test_windows_powershell_failure_returns_empty(self, install_mod):
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod.subprocess, "run") as run:
            run.return_value = type("R", (), {
                "returncode": 1, "stdout": "", "stderr": "no idea"})()
            assert install_mod._find_claude_hooks_pythonw_processes() == []


# ----------------------- _kill_pids_windows -------------------- #

class TestKillPidsWindows:
    def test_posix_is_no_op(self, install_mod):
        with patch.object(install_mod.os, "name", "posix"):
            assert install_mod._kill_pids_windows([1, 2, 3]) == 0

    def test_empty_list_is_no_op(self, install_mod):
        with patch.object(install_mod.os, "name", "nt"):
            assert install_mod._kill_pids_windows([]) == 0

    def test_returns_count_killed(self, install_mod):
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod.subprocess, "run") as run:
            run.return_value = type("R", (), {"returncode": 0})()
            assert install_mod._kill_pids_windows([10, 20, 30]) == 3

    def test_failed_kills_dont_count(self, install_mod):
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod.subprocess, "run") as run:
            results = iter([
                type("R", (), {"returncode": 0})(),
                type("R", (), {"returncode": 1})(),  # already dead, say
                type("R", (), {"returncode": 0})(),
            ])
            run.side_effect = lambda *a, **kw: next(results)
            assert install_mod._kill_pids_windows([10, 20, 30]) == 2


# ----------------------- _prune_stale_consultants_task --------- #

class TestPruneStaleConsultantsTask:
    def test_posix_returns_none(self, install_mod):
        with patch.object(install_mod.os, "name", "posix"):
            assert install_mod._prune_stale_consultants_task(
                service_mode="always-on",
                non_interactive=False, dry_run=False,
            ) is None

    def test_dry_run_only_prints(self, install_mod, capsys):
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod, "_windows_task_exists",
                          return_value=True):
            ret = install_mod._prune_stale_consultants_task(
                service_mode="always-on",
                non_interactive=False, dry_run=True,
            )
        assert ret is None
        out = capsys.readouterr().out
        assert "dry-run" in out
        assert "claude-hooks-consultants-forwarder" in out

    def test_non_interactive_reports_only(self, install_mod, capsys):
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod, "_windows_task_exists",
                          return_value=True):
            ret = install_mod._prune_stale_consultants_task(
                service_mode="always-on",
                non_interactive=True, dry_run=False,
            )
        assert ret is None
        out = capsys.readouterr().out
        assert "stale task" in out
        assert "interactively" in out

    def test_interactive_yes_deletes(self, install_mod, capsys):
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod, "_windows_task_exists",
                          return_value=True), \
             patch.object(install_mod.subprocess, "run") as run, \
             patch("builtins.input", return_value="y"):
            run.return_value = type("R", (), {
                "returncode": 0, "stdout": "ok", "stderr": ""})()
            ret = install_mod._prune_stale_consultants_task(
                service_mode="always-on",
                non_interactive=False, dry_run=False,
            )
        assert ret == "claude-hooks-consultants-forwarder"

    def test_interactive_default_accepts_yes(self, install_mod):
        # Empty input → default Y/yes
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod, "_windows_task_exists",
                          return_value=True), \
             patch.object(install_mod.subprocess, "run") as run, \
             patch("builtins.input", return_value=""):
            run.return_value = type("R", (), {
                "returncode": 0, "stdout": "", "stderr": ""})()
            ret = install_mod._prune_stale_consultants_task(
                service_mode="smart-start",
                non_interactive=False, dry_run=False,
            )
        # default-yes → opposite of smart-start is the always-on task
        assert ret == "claude-hooks-consultants"

    def test_interactive_no_skips(self, install_mod):
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod, "_windows_task_exists",
                          return_value=True), \
             patch("builtins.input", return_value="n"):
            ret = install_mod._prune_stale_consultants_task(
                service_mode="always-on",
                non_interactive=False, dry_run=False,
            )
        assert ret is None

    def test_no_stale_task_returns_none(self, install_mod):
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod, "_windows_task_exists",
                          return_value=False):
            ret = install_mod._prune_stale_consultants_task(
                service_mode="always-on",
                non_interactive=False, dry_run=False,
            )
        assert ret is None

    def test_unknown_service_mode_returns_none(self, install_mod):
        with patch.object(install_mod.os, "name", "nt"):
            ret = install_mod._prune_stale_consultants_task(
                service_mode="some-future-mode",
                non_interactive=False, dry_run=False,
            )
        assert ret is None


# ----------------------- _clear_pycache ------------------------ #

class TestClearPycache:
    def test_removes_pycache_dirs(self, install_mod, tmp_path):
        # Create nested __pycache__ dirs with stub .pyc files.
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__pycache__").mkdir()
        (tmp_path / "pkg" / "__pycache__" / "x.pyc").write_text("stub")
        (tmp_path / "pkg" / "sub").mkdir()
        (tmp_path / "pkg" / "sub" / "__pycache__").mkdir()
        (tmp_path / "pkg" / "sub" / "__pycache__" / "y.pyc").write_text("z")

        removed = install_mod._clear_pycache(tmp_path)
        assert removed == 2
        assert not (tmp_path / "pkg" / "__pycache__").exists()
        assert not (tmp_path / "pkg" / "sub" / "__pycache__").exists()
        # Source dirs survive.
        assert (tmp_path / "pkg").is_dir()
        assert (tmp_path / "pkg" / "sub").is_dir()

    def test_empty_tree_returns_zero(self, install_mod, tmp_path):
        (tmp_path / "src.py").write_text("")
        assert install_mod._clear_pycache(tmp_path) == 0

    def test_swallows_permission_errors(self, install_mod, tmp_path):
        # Force shutil.rmtree to raise; the helper should not propagate.
        (tmp_path / "__pycache__").mkdir()
        import shutil
        with patch.object(shutil, "rmtree",
                          side_effect=OSError("denied")):
            removed = install_mod._clear_pycache(tmp_path)
        assert removed == 0  # nothing actually removed


# ----------------------- _service_state_report ----------------- #

class TestServiceStateReport:
    def test_dry_run_returns_early(self, install_mod, capsys):
        install_mod._service_state_report(dry_run=True)
        out = capsys.readouterr().out
        assert "dry-run" in out

    def test_daemon_down_flagged(self, install_mod, capsys, monkeypatch):
        # Stub the import-from-inside path to return False.
        import types
        fake = types.SimpleNamespace(ping=lambda **kw: False)
        monkeypatch.setitem(
            __import__("sys").modules, "claude_hooks.daemon_client", fake)
        install_mod._service_state_report(dry_run=False)
        out = capsys.readouterr().out
        assert "claude-hooks-daemon" in out
        assert "not responding" in out
