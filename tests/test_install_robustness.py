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
        from pathlib import Path
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
                    "url": "http://192.168.178.2:38092/embedding",
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
                    "url": "http://192.168.178.2:38092/embedding",
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
        assert opts["url"] == "http://192.168.178.2:38092/embedding"
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
                    "url": "http://10.0.0.5:38092/embedding",
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
