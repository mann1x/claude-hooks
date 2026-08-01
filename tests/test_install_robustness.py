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
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tests._fixtures_net import (
    FIXTURE_LAN_HOST_ALT,
    FIXTURE_LLAMAFILE_URL,
)


@pytest.fixture(scope="module")
def install_mod():
    """Load install.py as a module so we can poke its internals
    without invoking ``main()``."""
    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "install", repo_root / "install.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: install.py is ``from __future__ import
    # annotations``, so ``@dataclasses.dataclass`` resolves its string
    # annotations through ``sys.modules[cls.__module__]``. Without this
    # the whole file errors at fixture setup unless some earlier test
    # module happened to ``import install`` first — a collection-order
    # dependency that made this file unrunnable on its own.
    sys.modules.setdefault("install", mod)
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
        # Existence sequence: True (initial check) → False (post-delete
        # verification). The delete itself goes through
        # _run_schtasks_elevated (UAC-elevated), not bare subprocess —
        # mirrors the create-side elevation. _force_kill_task_processes
        # also fires before the delete to clean any orphan pythonw.
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod, "_windows_task_exists",
                          side_effect=[True, False]), \
             patch.object(install_mod, "_force_kill_task_processes") as kill, \
             patch.object(install_mod, "_run_schtasks_elevated",
                          return_value=True) as elevated, \
             patch("builtins.input", return_value="y"):
            ret = install_mod._prune_stale_consultants_task(
                service_mode="always-on",
                non_interactive=False, dry_run=False,
            )
        assert ret == "claude-hooks-consultants-forwarder"
        # Opposite of always-on is the smart-start forwarder, which
        # binds 38096. The kill helper must be called with that port
        # so the running pythonw is reaped before the task delete.
        kill.assert_called_once_with(
            "claude-hooks-consultants-forwarder", 38096,
        )
        # Delete went through the elevated path.
        elevated.assert_called_once()

    def test_interactive_default_accepts_yes(self, install_mod):
        # Empty input → default Y/yes. service_mode=smart-start → the
        # opposite task is claude-hooks-consultants (always-on, port
        # 38095).
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod, "_windows_task_exists",
                          side_effect=[True, False]), \
             patch.object(install_mod, "_force_kill_task_processes") as kill, \
             patch.object(install_mod, "_run_schtasks_elevated",
                          return_value=True), \
             patch("builtins.input", return_value=""):
            ret = install_mod._prune_stale_consultants_task(
                service_mode="smart-start",
                non_interactive=False, dry_run=False,
            )
        # default-yes → opposite of smart-start is the always-on task
        assert ret == "claude-hooks-consultants"
        kill.assert_called_once_with("claude-hooks-consultants", 38095)

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

    def test_uac_declined_returns_none(self, install_mod, capsys):
        # _run_schtasks_elevated returns False when the user declines
        # the UAC prompt (or schtasks itself errors). The function
        # must surface the failure and return None — no spurious
        # "deleted" report.
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod, "_windows_task_exists",
                          return_value=True), \
             patch.object(install_mod, "_force_kill_task_processes"), \
             patch.object(install_mod, "_run_schtasks_elevated",
                          return_value=False), \
             patch("builtins.input", return_value="y"):
            ret = install_mod._prune_stale_consultants_task(
                service_mode="always-on",
                non_interactive=False, dry_run=False,
            )
        assert ret is None
        out = capsys.readouterr().out
        assert "UAC declined" in out or "failed to delete" in out

    def test_elevated_succeeded_but_task_still_exists(self, install_mod, capsys):
        # _run_schtasks_elevated may return True via the
        # ``Start-Process -Wait`` path even when the child schtasks
        # actually failed (rc is hidden by Start-Process). Function
        # must re-verify and report when the task is still present.
        with patch.object(install_mod.os, "name", "nt"), \
             patch.object(install_mod, "_windows_task_exists",
                          side_effect=[True, True]), \
             patch.object(install_mod, "_force_kill_task_processes"), \
             patch.object(install_mod, "_run_schtasks_elevated",
                          return_value=True), \
             patch("builtins.input", return_value="y"):
            ret = install_mod._prune_stale_consultants_task(
                service_mode="always-on",
                non_interactive=False, dry_run=False,
            )
        assert ret is None
        out = capsys.readouterr().out
        assert "still exists after /Delete" in out

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


# ----------------------- _prompt_consultants_service_mode ------ #
#
# Regression pin for the 2026-05-21 bug where the service-mode
# prompt hardcoded ``"always-on"`` as the default, so piping ``\n``
# on stdin silently flipped configured-smart-start hosts to
# always-on. The fix reads the currently-configured mode via
# ``_detect_consultants_service_mode`` and uses it as the default
# and the prompt marker.

class TestPromptConsultantsServiceMode:
    def _smart_cfg(self) -> dict:
        return {"hooks": {"consultants": {
            "smart_start": {"enabled": True}}}}

    def _always_cfg(self) -> dict:
        return {"hooks": {"consultants": {
            "smart_start": {"enabled": False}}}}

    def test_non_interactive_preserves_smart_start(self, install_mod):
        # Pre-fix bug: non-interactive forced always-on. Post-fix
        # must keep whatever is configured.
        assert install_mod._prompt_consultants_service_mode(
            self._smart_cfg(), non_interactive=True,
        ) == "smart-start"

    def test_non_interactive_preserves_always_on(self, install_mod):
        assert install_mod._prompt_consultants_service_mode(
            self._always_cfg(), non_interactive=True,
        ) == "always-on"

    def test_empty_input_keeps_smart_start_default(self, install_mod):
        # The reproducer: piping ``\n`` to stdin must NOT flip a
        # configured smart-start host to always-on.
        with patch("builtins.input", return_value=""):
            assert install_mod._prompt_consultants_service_mode(
                self._smart_cfg(), non_interactive=False,
            ) == "smart-start"

    def test_empty_input_keeps_always_on_default(self, install_mod):
        with patch("builtins.input", return_value=""):
            assert install_mod._prompt_consultants_service_mode(
                self._always_cfg(), non_interactive=False,
            ) == "always-on"

    def test_user_picks_smart_start_explicitly(self, install_mod):
        with patch("builtins.input", return_value="s"):
            assert install_mod._prompt_consultants_service_mode(
                self._always_cfg(), non_interactive=False,
            ) == "smart-start"

    def test_user_picks_always_on_explicitly(self, install_mod):
        with patch("builtins.input", return_value="a"):
            assert install_mod._prompt_consultants_service_mode(
                self._smart_cfg(), non_interactive=False,
            ) == "always-on"

    def test_unrecognized_input_keeps_current(self, install_mod):
        # Garbage input must not silently flip — keep current_mode.
        with patch("builtins.input", return_value="xyz"):
            assert install_mod._prompt_consultants_service_mode(
                self._smart_cfg(), non_interactive=False,
            ) == "smart-start"

    def test_prompt_marker_reflects_always_on(self, install_mod, capsys):
        captured = []
        with patch("builtins.input",
                   side_effect=lambda prompt: (captured.append(prompt) or "")):
            install_mod._prompt_consultants_service_mode(
                self._always_cfg(), non_interactive=False,
            )
        assert captured, "input prompt was never called"
        assert "[A/s]" in captured[0]
        assert "Current: always-on" in captured[0]

    def test_prompt_marker_reflects_smart_start(self, install_mod):
        # Mirror of the always-on test for the smart-start branch.
        captured = []
        with patch("builtins.input",
                   side_effect=lambda prompt: (captured.append(prompt) or "")):
            install_mod._prompt_consultants_service_mode(
                self._smart_cfg(), non_interactive=False,
            )
        assert captured
        assert "[a/S]" in captured[0]
        assert "Current: smart-start" in captured[0]

    def test_uppercase_input_recognized(self, install_mod):
        # ``"S"`` (capital) must be recognized as smart-start too —
        # the function lower-cases before matching.
        with patch("builtins.input", return_value="S"):
            assert install_mod._prompt_consultants_service_mode(
                self._always_cfg(), non_interactive=False,
            ) == "smart-start"


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


# ----------------------- #223: service-mode helpers ------------- #

class TestDetectConsultantsServiceMode:
    """``_detect_consultants_service_mode(cfg)`` reads the smart_start
    flag from ``hooks.consultants.smart_start.enabled`` and returns the
    matching service-mode label. Backwards-compatible for the missing-
    key cases — installs without the consultants block fall back to
    'always-on'."""

    def test_empty_cfg_falls_back_to_always_on(self, install_mod):
        assert install_mod._detect_consultants_service_mode({}) == "always-on"

    def test_non_dict_cfg_falls_back_to_always_on(self, install_mod):
        # Defensive — caller might pass None or a string accidentally.
        assert install_mod._detect_consultants_service_mode(None) == "always-on"  # type: ignore[arg-type]

    def test_missing_consultants_block_is_always_on(self, install_mod):
        cfg = {"hooks": {"other": {}}}
        assert install_mod._detect_consultants_service_mode(cfg) == "always-on"

    def test_smart_start_enabled_true_returns_smart_start(self, install_mod):
        cfg = {"hooks": {"consultants": {"smart_start": {"enabled": True}}}}
        assert install_mod._detect_consultants_service_mode(cfg) == "smart-start"

    def test_smart_start_enabled_false_returns_always_on(self, install_mod):
        cfg = {"hooks": {"consultants": {"smart_start": {"enabled": False}}}}
        assert install_mod._detect_consultants_service_mode(cfg) == "always-on"

    def test_smart_start_block_present_but_no_enabled_key(self, install_mod):
        # The block exists but the flag was never set — treated as
        # always-on so the installer's restart logic doesn't try to
        # speak to a forwarder that was never registered.
        cfg = {"hooks": {"consultants": {"smart_start": {}}}}
        assert install_mod._detect_consultants_service_mode(cfg) == "always-on"


class TestConsultantsRestartTarget:
    """``_consultants_restart_target(mode)`` resolves the schtasks task
    name, the health-check port, and the friendly label for the chosen
    service mode. Used by ``_restart_consultants_service`` so it talks
    to the right task and probes the right port."""

    def test_always_on_returns_engine_task(self, install_mod):
        task, port, label = install_mod._consultants_restart_target("always-on")
        assert task == install_mod._CONSULTANTS_TASK_NAME
        assert port == 38095
        assert "always-on" in label

    def test_smart_start_returns_forwarder_task(self, install_mod):
        task, port, label = install_mod._consultants_restart_target("smart-start")
        assert task == install_mod._CONSULTANTS_FORWARDER_TASK_NAME
        assert port == 38096
        assert "smart-start" in label

    def test_unknown_mode_defaults_to_always_on(self, install_mod):
        # Defensive fall-through — unrecognised modes (typos, future
        # values from a newer config) shouldn't crash; they get the
        # safe-default path. Pre-#223 the restart logic was always-on-
        # only, so unknown == always-on preserves that contract.
        task, port, _ = install_mod._consultants_restart_target("frobnicate")
        assert task == install_mod._CONSULTANTS_TASK_NAME
        assert port == 38095


class TestServiceStateReportDriftAware:
    """``_service_state_report`` (post-#223) takes an optional ``cfg``
    so it can probe the EXPECTED port first based on the configured
    mode and surface drift when a different port is actually serving.
    """

    def test_dry_run_with_cfg_still_returns_early(
            self, install_mod, capsys):
        install_mod._service_state_report(
            dry_run=True,
            cfg={"hooks": {"consultants": {"smart_start": {"enabled": True}}}},
        )
        out = capsys.readouterr().out
        assert "dry-run" in out

    def test_cfg_none_does_not_crash(
            self, install_mod, capsys, monkeypatch):
        # When cfg is None, _detect_consultants_service_mode falls back
        # to always-on; ensure the function still completes cleanly
        # without TypeError.
        import types
        fake = types.SimpleNamespace(ping=lambda **kw: True)
        monkeypatch.setitem(
            __import__("sys").modules, "claude_hooks.daemon_client", fake)
        install_mod._service_state_report(dry_run=False, cfg=None)
        out = capsys.readouterr().out
        assert "claude-hooks-daemon" in out


class TestSyncConsultantsServiceMode:
    """``_sync_consultants_service_mode`` writes the chosen mode into
    ``~/.claude/consultants-config.toml`` via the consultants-env
    ``set_service_mode`` mutator. Best-effort: a missing env or a
    failed subprocess prints a warning and doesn't raise.
    """

    def test_dry_run_prints_intent_and_returns(
            self, install_mod, tmp_path, capsys):
        install_mod._sync_consultants_service_mode(
            "smart-start",
            consultants_py=tmp_path / "py",
            dry_run=True,
        )
        out = capsys.readouterr().out
        assert "dry-run" in out
        assert "smart-start" in out

    def test_missing_consultants_py_warns_does_not_raise(
            self, install_mod, tmp_path, capsys):
        # Env not installed yet — best-effort path: print + return.
        nonexistent = tmp_path / "absent-env" / "python"
        install_mod._sync_consultants_service_mode(
            "always-on",
            consultants_py=nonexistent,
            dry_run=False,
        )
        out = capsys.readouterr().out
        assert "consultants env python not found" in out
        assert "claude-consultants config set-service-mode" in out


# ----------------------- service-mode drift (2026-08-01) -------- #
#
# pandorum ran smart-start for weeks while its
# ~/.claude/consultants-config.toml still said "always-on", and every
# deploy left it that way. Two compounding causes:
#
#   1. install.py resolved the mode from the MIRROR
#      (config/claude-hooks.json hooks.consultants.smart_start.enabled)
#      and never read the TOML the operator actually edits with
#      `claude-consultants config set-service-mode`. So a mode set
#      through the documented CLI was invisible to the installer and
#      got written back to the stale value on the next deploy.
#   2. All of the mode handling — drift detection, the prompt, the
#      TOML sync — sat behind the "Refresh /consultants engine deps?
#      [y/N]" gate, which a routine deploy answers no to. Nothing
#      self-healed.
#
# The fix reads the TOML directly and reconciles the mirror to it,
# before the gate.

class TestReadUserConsultantsServiceMode:
    """``_read_user_consultants_service_mode`` — a stdlib, env-free
    read of ``[service].mode`` from the user-global consultants TOML."""

    def _write(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "consultants-config.toml"
        p.write_text(body, encoding="utf-8")
        return p

    def test_reads_smart_start(self, install_mod, tmp_path):
        p = self._write(tmp_path,
                        'topology = "star"\n\n'
                        '[service]\nmode = "smart-start"\nhttp_port = 38095\n')
        assert install_mod._read_user_consultants_service_mode(p) \
            == "smart-start"

    def test_reads_always_on(self, install_mod, tmp_path):
        p = self._write(tmp_path, '[service]\nmode = "always-on"\n')
        assert install_mod._read_user_consultants_service_mode(p) \
            == "always-on"

    def test_missing_file_is_none(self, install_mod, tmp_path):
        assert install_mod._read_user_consultants_service_mode(
            tmp_path / "absent.toml") is None

    def test_unknown_mode_is_none(self, install_mod, tmp_path):
        # A typo must read as "no opinion" so the caller keeps the
        # mirror rather than registering a task for a mode that has no
        # task.
        p = self._write(tmp_path, '[service]\nmode = "frobnicate"\n')
        assert install_mod._read_user_consultants_service_mode(p) is None

    def test_missing_service_table_is_none(self, install_mod, tmp_path):
        p = self._write(tmp_path, 'topology = "star"\neffort = "high"\n')
        assert install_mod._read_user_consultants_service_mode(p) is None

    def test_mode_key_in_another_table_is_not_picked_up(
            self, install_mod, tmp_path):
        # ``[coder] mode = ...`` is a different knob entirely; a naive
        # whole-file regex would return it.
        p = self._write(
            tmp_path,
            '[coder]\nmode = "smart-start"\n\n'
            '[service]\nmode = "always-on"\n')
        assert install_mod._read_user_consultants_service_mode(p) \
            == "always-on"

    def test_unparseable_toml_falls_back_to_the_scan(
            self, install_mod, tmp_path):
        # Hand-edited file with a syntax error further down: tomllib
        # raises, the scoped scan still finds the mode. Losing the
        # mode over an unrelated typo would resurrect the drift.
        p = self._write(
            tmp_path,
            '[service]\nmode = "smart-start"\n\n'
            '[store]\nthis is not toml\n')
        assert install_mod._read_user_consultants_service_mode(p) \
            == "smart-start"


class TestReconcileConsultantsServiceMode:
    """``_reconcile_consultants_service_mode`` — the TOML wins, and the
    JSON mirror is rewritten to match."""

    @pytest.fixture
    def toml_at(self, install_mod, tmp_path, monkeypatch):
        """Point the reconciler at a temp TOML; return a writer."""
        p = tmp_path / "consultants-config.toml"

        def _write(mode: str | None) -> Path:
            if mode is not None:
                p.write_text(f'[service]\nmode = "{mode}"\n',
                             encoding="utf-8")
            return p

        monkeypatch.setattr(install_mod, "_user_consultants_config_path",
                            lambda: p)
        return _write

    def _cfg(self, smart_enabled: bool) -> dict:
        return {"hooks": {"consultants": {
            "smart_start": {"enabled": smart_enabled}}}}

    def test_toml_smart_start_overrides_always_on_mirror(
            self, install_mod, tmp_path, toml_at):
        # The pandorum reproducer, in the direction that bit: the
        # operator set smart-start via the CLI, the mirror still says
        # always-on.
        toml_at("smart-start")
        cfg = self._cfg(False)
        cfg_path = tmp_path / "claude-hooks.json"
        assert install_mod._reconcile_consultants_service_mode(
            cfg, cfg_path, dry_run=False) == "smart-start"
        assert cfg["hooks"]["consultants"]["smart_start"]["enabled"] is True
        # Persisted, not just mutated in memory — the next deploy reads
        # the file, not this dict.
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["hooks"]["consultants"]["smart_start"]["enabled"] \
            is True

    def test_toml_always_on_overrides_smart_start_mirror(
            self, install_mod, tmp_path, toml_at):
        toml_at("always-on")
        cfg = self._cfg(True)
        cfg_path = tmp_path / "claude-hooks.json"
        assert install_mod._reconcile_consultants_service_mode(
            cfg, cfg_path, dry_run=False) == "always-on"
        assert cfg["hooks"]["consultants"]["smart_start"]["enabled"] is False

    def test_agreement_is_a_no_op(self, install_mod, tmp_path, toml_at,
                                  capsys):
        toml_at("always-on")
        cfg = self._cfg(False)
        cfg_path = tmp_path / "claude-hooks.json"
        assert install_mod._reconcile_consultants_service_mode(
            cfg, cfg_path, dry_run=False) == "always-on"
        # No rewrite, no drift banner — the common case must stay quiet.
        assert not cfg_path.exists()
        assert "drift" not in capsys.readouterr().out

    def test_no_toml_leaves_the_mirror_alone(
            self, install_mod, tmp_path, toml_at):
        toml_at(None)  # never written
        cfg = self._cfg(True)
        cfg_path = tmp_path / "claude-hooks.json"
        assert install_mod._reconcile_consultants_service_mode(
            cfg, cfg_path, dry_run=False) is None
        assert cfg["hooks"]["consultants"]["smart_start"]["enabled"] is True
        assert not cfg_path.exists()

    def test_missing_consultants_block_gets_created(
            self, install_mod, tmp_path, toml_at):
        toml_at("smart-start")
        cfg: dict = {}
        cfg_path = tmp_path / "claude-hooks.json"
        assert install_mod._reconcile_consultants_service_mode(
            cfg, cfg_path, dry_run=False) == "smart-start"
        assert cfg["hooks"]["consultants"]["smart_start"]["enabled"] is True

    def test_dry_run_reports_but_does_not_write(
            self, install_mod, tmp_path, toml_at, capsys):
        toml_at("smart-start")
        cfg = self._cfg(False)
        cfg_path = tmp_path / "claude-hooks.json"
        assert install_mod._reconcile_consultants_service_mode(
            cfg, cfg_path, dry_run=True) == "smart-start"
        assert cfg["hooks"]["consultants"]["smart_start"]["enabled"] is False
        assert not cfg_path.exists()
        assert "dry-run" in capsys.readouterr().out

    def test_unwritable_mirror_warns_and_still_returns_the_mode(
            self, install_mod, tmp_path, toml_at, capsys):
        toml_at("smart-start")
        cfg = self._cfg(False)
        # A directory where the file should be → OSError on write.
        cfg_path = tmp_path / "wedged"
        cfg_path.mkdir()
        assert install_mod._reconcile_consultants_service_mode(
            cfg, cfg_path, dry_run=False) == "smart-start"
        assert "could not write" in capsys.readouterr().out

    def test_reconciled_mirror_drives_the_non_interactive_prompt(
            self, install_mod, tmp_path, toml_at):
        # The end-to-end property: after reconciliation, the mode a
        # --non-interactive deploy resolves is the operator's TOML
        # value. Pre-fix this returned the stale mirror and the deploy
        # wrote it back over the TOML.
        toml_at("smart-start")
        cfg = self._cfg(False)
        install_mod._reconcile_consultants_service_mode(
            cfg, tmp_path / "claude-hooks.json", dry_run=False)
        assert install_mod._prompt_consultants_service_mode(
            cfg, non_interactive=True) == "smart-start"

    def test_reconcile_runs_before_the_refresh_gate(self, install_mod):
        # Structural pin for cause (2): every mode-handling call must
        # stay ABOVE the "Refresh /consultants engine deps?" prompt,
        # because a routine deploy answers no and returns early.
        import inspect
        src = inspect.getsource(install_mod._install_consultants)
        assert src.index("_reconcile_consultants_service_mode") \
            < src.index("Refresh /consultants engine deps?")


# ----- #237: install.py audit (2026-05-21) ----------------------------- #
#
# Coordinated tests for four findings landed together:
#   1. Stale docstring at _setup_sqlite_vec_mcp (smoke: must mention
#      both "MCP launcher" AND "schema migration" so the v1.6+/v1.7+
#      capabilities aren't denied).
#   2. _validate_sqlite_vec_only — read-only probe, no mutations to
#      cfg, handles missing db_path / missing file / pre-v1.7 schema.
#   3. Remote llamafile primary path in _setup_embedding_engine —
#      saves embedder="llamafile" with daemon_ensure=false and
#      removes any stale local cfg["embedding"] block.
#   4. LAN-exposure prompt in _setup_llamafile_engine — default
#      loopback, opt-in 0.0.0.0, preserves existing on re-run.


class TestSqliteVecDocstringNotStale:
    """Finding 1 — sentinel test for the docstring claims. If a
    future regression re-introduces the pre-v1.6 wording, this
    catches it before users see misleading guidance."""

    def test_docstring_mentions_mcp_launcher(self, install_mod):
        doc = install_mod._setup_sqlite_vec_mcp.__doc__ or ""
        assert "MCP launcher" in doc, (
            "v1.6+ ships a system-wide MCP launcher; docstring must "
            "not claim 'no MCP server, no system-wide launcher'."
        )

    def test_docstring_mentions_schema_migration(self, install_mod):
        doc = install_mod._setup_sqlite_vec_mcp.__doc__ or ""
        assert "schema migration" in doc, (
            "v1.7+ ships lazy schema migration; docstring must not "
            "claim 'no schema migration'."
        )


class TestValidateSqliteVecOnly:
    """Finding 2 — read-only validator twin of _validate_pgvector_only."""

    def test_no_db_path_in_cfg_prints_and_returns(self, install_mod, capsys):
        install_mod._validate_sqlite_vec_only({})
        out = capsys.readouterr().out
        assert "No db_path in config" in out

    def test_missing_db_file_is_handled(
            self, install_mod, tmp_path, capsys):
        missing = tmp_path / "absent.db"
        install_mod._validate_sqlite_vec_only({
            "providers": {"sqlite_vec": {"db_path": str(missing)}},
        })
        out = capsys.readouterr().out
        assert "does not exist" in out
        assert "nothing to validate" in out

    def test_pre_v17_db_reports_pre_v17_schema(
            self, install_mod, tmp_path, capsys):
        # Create a SQLite file without the v1.7 bookkeeping table.
        import sqlite3
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE legacy(x INTEGER)")
        conn.commit()
        conn.close()
        install_mod._validate_sqlite_vec_only({
            "providers": {"sqlite_vec": {
                "db_path": str(db),
                "embedder": "llamafile",
                "embedder_options": {
                    "url": "http://127.0.0.1:38092/embedding",
                    "daemon_ensure": True,
                },
            }},
        })
        out = capsys.readouterr().out
        assert "pre-v1.7" in out
        # Daemon-managed llamafile is the most common embedder kind on
        # solidpc-style hosts; verify the dispatch branch fires.
        assert "daemon-managed" in out

    def test_v17_db_reports_schema_version_and_launcher(
            self, install_mod, tmp_path, capsys, monkeypatch):
        # Create a sqlite db with the v1.7 bookkeeping shape.
        import sqlite3
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE claude_hooks_schema(version INTEGER)")
        conn.execute("INSERT INTO claude_hooks_schema(version) VALUES (?)", (2,))
        conn.commit()
        conn.close()
        # Stub the launcher path so the assertion is host-independent.
        fake_launcher = tmp_path / "fake-launcher"
        fake_launcher.write_text("#!/bin/sh\necho launcher\n")
        monkeypatch.setattr(
            install_mod, "_sqlite_vec_launcher_path",
            lambda: fake_launcher,
        )
        install_mod._validate_sqlite_vec_only({
            "providers": {"sqlite_vec": {
                "db_path": str(db),
                "embedder": "llamafile",
                "embedder_options": {
                    "url": FIXTURE_LLAMAFILE_URL,
                    "daemon_ensure": False,
                },
            }},
        })
        out = capsys.readouterr().out
        assert "schema version: v2" in out
        assert "present" in out
        # Remote-llamafile dispatch surfaces the no-supervision label
        # so the operator sees the LAN topology without checking config.
        assert "remote, no local supervision" in out


class TestRemoteLlamafilePrimaryDialog:
    """Finding 3 — non-interactive convergence test for the new
    remote-llamafile-as-primary branch in _setup_embedding_engine.
    """

    def test_existing_remote_llamafile_preserved_non_interactive(
            self, install_mod):
        # Simulate a host already configured with a remote llamafile
        # primary; --non-interactive must keep it.
        cfg = {
            "providers": {"pgvector": {
                "embedder": "llamafile",
                "embedder_options": {
                    "url": FIXTURE_LLAMAFILE_URL,
                    "daemon_ensure": False,
                    "timeout": 30.0,
                },
            }},
            # Stale local embedding block from a previous local install
            # — the remote-primary branch must strip it.
            "embedding": {"enabled": True, "llamafile_path": "/nope"},
        }
        # Force the Ollama branch off so we hit the remote-llamafile branch.
        install_mod._setup_embedding_engine(
            cfg, provider="pgvector",
            non_interactive=True, dry_run=False,
        )
        # The non-interactive code path falls through Ollama=true by
        # default because existing_kind="llamafile" doesn't match the
        # "ollama"/"composite" check at the top — so it takes the
        # "use_ollama=False" branch, sees use_remote_llamafile=True
        # from the existing config, and re-saves the same shape.
        opts = cfg["providers"]["pgvector"]["embedder_options"]
        assert cfg["providers"]["pgvector"]["embedder"] == "llamafile"
        assert opts["url"] == FIXTURE_LLAMAFILE_URL
        assert opts["daemon_ensure"] is False
        assert "embedding" not in cfg, (
            "remote-primary path must strip a stale local embedding block"
        )

    def test_helper_strips_stale_local_embedding_block(self, install_mod):
        # Just verifies the post-condition documented in Finding 3:
        # remote-llamafile primary == no local supervision required.
        # We exercise the same path as the test above but with a
        # different stale-block shape to guard against the cleanup
        # being too narrow.
        cfg = {
            "providers": {"pgvector": {
                "embedder": "llamafile",
                "embedder_options": {
                    "url": f"http://{FIXTURE_LAN_HOST_ALT}:38092/embedding",
                    "daemon_ensure": False,
                },
            }},
            "embedding": {
                "enabled": True,
                "host": "0.0.0.0",  # was producer; now consumer.
                "llamafile_path": "/legacy/path",
                "port": 38092,
            },
        }
        install_mod._setup_embedding_engine(
            cfg, provider="pgvector",
            non_interactive=True, dry_run=False,
        )
        assert "embedding" not in cfg


class TestLanExposurePrompt:
    """Finding 4 — LAN exposure prompt in _setup_llamafile_engine.
    Verifies the default-loopback behavior + the round-trip of an
    existing 0.0.0.0 setting across re-installs.
    """

    def test_non_interactive_default_keeps_loopback(
            self, install_mod, tmp_path, monkeypatch, capsys):
        # Skip composite fetch + GPU probe paths by stubbing them.
        cfg = {"embedding": {}}  # no existing host -> default loopback
        # Stub the GPU probe + composite fetch so the unit doesn't
        # touch the filesystem / network. mode=cpu is the simplest
        # non-interactive branch.
        monkeypatch.setattr(
            install_mod, "_download_composite_llamafile",
            lambda *a, **kw: True,
        )
        block = install_mod._setup_llamafile_engine(
            cfg, non_interactive=True, dry_run=True,
        )
        assert block["host"] == "127.0.0.1", (
            "fresh non-interactive install must default to loopback"
        )

    def test_non_interactive_preserves_existing_lan_host(
            self, install_mod, monkeypatch):
        # A host that's already set to 0.0.0.0 must NOT be reset to
        # loopback by a --non-interactive re-run.
        cfg = {"embedding": {"host": "0.0.0.0"}}
        monkeypatch.setattr(
            install_mod, "_download_composite_llamafile",
            lambda *a, **kw: True,
        )
        block = install_mod._setup_llamafile_engine(
            cfg, non_interactive=True, dry_run=True,
        )
        assert block["host"] == "0.0.0.0", (
            "non-interactive re-run must preserve existing LAN exposure"
        )

    def test_block_shape_includes_host_field(self, install_mod, monkeypatch):
        # Schema sentinel: the returned block must always carry "host"
        # so callers (and the EmbeddingConfig dataclass) get a stable
        # field set across the v1.4 (no host) -> #237 (host) transition.
        cfg = {}
        monkeypatch.setattr(
            install_mod, "_download_composite_llamafile",
            lambda *a, **kw: True,
        )
        block = install_mod._setup_llamafile_engine(
            cfg, non_interactive=True, dry_run=True,
        )
        for key in (
                "enabled", "llamafile_path", "model_gguf",
                "host", "port", "ctx_size", "pooling",
                "mode", "idle_timeout_seconds"):
            assert key in block, f"missing key in block: {key!r}"
