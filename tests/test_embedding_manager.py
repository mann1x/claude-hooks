"""Tests for claude_hooks.embedding_manager (v1.4).

The manager wraps three independently-mockable surfaces:

- ``subprocess.Popen`` (the llamafile child)
- ``urllib.request.urlopen`` (the /health probe)
- ``claude_hooks.gpu_probe`` (the VRAM check)

We script each separately. No real llamafile is launched; the
``FakeProc`` mocks just enough of the ``Popen`` interface to drive
the lifecycle state machine.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from claude_hooks import embedding_manager as em


# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #

class FakeProc:
    """Minimal Popen-shape stub. Stays 'alive' (poll()==None) until
    .terminate() or .kill() is called, then returns the configured
    rc."""

    def __init__(self, pid: int = 12345, terminate_rc: int = 0):
        self.pid = pid
        self.returncode = None
        self._terminate_rc = terminate_rc
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = self._terminate_rc

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None:
            # Pretend we won't ever exit until terminate/kill — let
            # the caller decide via TimeoutExpired.
            raise subprocess.TimeoutExpired("llamafile", timeout or 0)
        return self.returncode


class FakeHealthResp:
    def __init__(self, status: int = 200):
        self.status = status
        self._buf = BytesIO(b"{\"status\":\"ok\"}")

    def read(self, *a, **kw):
        return self._buf.read(*a, **kw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._buf.close()


def _base_cfg(**overrides) -> em.EmbeddingConfig:
    base = dict(
        llamafile_path="/path/to/llamafile",
        port=38199,
        ctx_size=16384,
        pooling="last",
        mode="auto",
        spawn_timeout_seconds=3.0,
        idle_timeout_seconds=300.0,
        reaper_interval_seconds=60.0,
    )
    base.update(overrides)
    return em.EmbeddingConfig(**base)


# --------------------------------------------------------------------- #
# Config + construction
# --------------------------------------------------------------------- #

class TestConfig:
    def test_invalid_pooling_rejected(self):
        with pytest.raises(ValueError, match="invalid pooling"):
            em.EmbeddingManager(_base_cfg(pooling="invalid"))

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="invalid mode"):
            em.EmbeddingManager(_base_cfg(mode="rocket"))

    def test_config_from_dict_full(self):
        cfg = em.config_from_dict({"embedding": {
            "llamafile_path": "/x",
            "model_gguf": "/y.gguf",
            "host": "127.0.0.1",
            "port": 12345,
            "ctx_size": 8192,
            "pooling": "mean",
            "mode": "cpu",
            "vram_budget_mb": 4096,
            "spawn_timeout_seconds": 45.0,
            "idle_timeout_seconds": 600.0,
            "reaper_interval_seconds": 30.0,
            "extra_args": ["--foo", "bar"],
            "cwd": "/tmp",
        }})
        assert cfg.llamafile_path == "/x"
        assert cfg.model_gguf == "/y.gguf"
        assert cfg.port == 12345
        assert cfg.ctx_size == 8192
        assert cfg.pooling == "mean"
        assert cfg.mode == "cpu"
        assert cfg.vram_budget_mb == 4096
        assert cfg.idle_timeout_seconds == 600.0
        assert cfg.extra_args == ["--foo", "bar"]

    def test_config_from_dict_defaults(self):
        cfg = em.config_from_dict({})
        assert cfg.llamafile_path == ""
        assert cfg.port == 38092
        assert cfg.ctx_size == 16384
        assert cfg.pooling == "last"
        assert cfg.mode == "auto"
        assert cfg.idle_timeout_seconds == 300.0
        assert cfg.extra_args == []

    def test_config_from_dict_ignores_unknown(self):
        cfg = em.config_from_dict({"embedding": {"some_unknown_key": 1,
                                                 "port": 999}})
        assert cfg.port == 999


# --------------------------------------------------------------------- #
# _build_cmd / _gpu_flags
# --------------------------------------------------------------------- #

class TestBuildCmd:
    def test_path_missing_raises(self):
        # llamafile_path empty
        m = em.EmbeddingManager(_base_cfg(llamafile_path=""))
        with pytest.raises(RuntimeError, match="not set"):
            m._build_cmd()

    def test_path_not_a_file_raises(self, tmp_path):
        m = em.EmbeddingManager(_base_cfg(llamafile_path=str(tmp_path / "nope")))
        with pytest.raises(RuntimeError, match="not found"):
            m._build_cmd()

    def test_command_shape_composite(self, tmp_path):
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"#!/bin/true\n")
        m = em.EmbeddingManager(_base_cfg(llamafile_path=str(binp), mode="cpu"))
        cmd = m._build_cmd()
        # Canonical pieces present, in expected order:
        assert cmd[0] == str(binp)
        assert "--server" in cmd
        assert "--host" in cmd and "127.0.0.1" in cmd
        assert "--port" in cmd and "38199" in cmd
        assert "--embedding" in cmd
        assert "--pooling" in cmd and "last" in cmd
        assert "--ctx-size" in cmd and "16384" in cmd
        # CPU mode injects --gpu disable, NOT -ngl
        assert "--gpu" in cmd and "disable" in cmd
        assert "-ngl" not in cmd
        # No -m flag because no model_gguf set (composite case).
        assert "-m" not in cmd

    def test_command_shape_custom_gguf(self, tmp_path):
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"")
        m = em.EmbeddingManager(_base_cfg(
            llamafile_path=str(binp),
            model_gguf="/models/foo.gguf",
            mode="cpu",
        ))
        cmd = m._build_cmd()
        assert "-m" in cmd
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "/models/foo.gguf"

    def test_command_shape_auto_gpu_fits(self, tmp_path):
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"")
        m = em.EmbeddingManager(_base_cfg(llamafile_path=str(binp), mode="auto"))
        from claude_hooks import gpu_probe
        with patch.object(gpu_probe, "can_fit_in_vram", return_value=True):
            cmd = m._build_cmd()
        assert "-ngl" in cmd
        idx = cmd.index("-ngl")
        assert cmd[idx + 1] == "99"
        assert "--gpu" not in cmd

    def test_command_shape_auto_doesnt_fit_falls_to_cpu(self, tmp_path):
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"")
        m = em.EmbeddingManager(_base_cfg(llamafile_path=str(binp), mode="auto"))
        from claude_hooks import gpu_probe
        with patch.object(gpu_probe, "can_fit_in_vram", return_value=False):
            cmd = m._build_cmd()
        assert "--gpu" in cmd and "disable" in cmd
        assert "-ngl" not in cmd

    def test_command_shape_auto_unknown_vram_tries_gpu(self, tmp_path):
        """can_fit_in_vram=None (vulkan or no probe) — fall through to
        runtime probe, pass -ngl 99 and let llamafile sort it out."""
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"")
        m = em.EmbeddingManager(_base_cfg(llamafile_path=str(binp), mode="auto"))
        from claude_hooks import gpu_probe
        with patch.object(gpu_probe, "can_fit_in_vram", return_value=None):
            cmd = m._build_cmd()
        assert "-ngl" in cmd

    def test_command_shape_gpu_offload_failed_sticky(self, tmp_path):
        """Once gpu_offload_failed=True, every subsequent build is
        CPU-only even though mode is still 'auto'."""
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"")
        m = em.EmbeddingManager(_base_cfg(llamafile_path=str(binp), mode="auto"))
        m.gpu_offload_failed = True
        # can_fit_in_vram shouldn't even be consulted; patch to a
        # value that would otherwise produce -ngl to prove the
        # sticky flag wins.
        from claude_hooks import gpu_probe
        with patch.object(gpu_probe, "can_fit_in_vram", return_value=True):
            cmd = m._build_cmd()
        assert "--gpu" in cmd
        assert "-ngl" not in cmd

    def test_extra_args_appended(self, tmp_path):
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"")
        m = em.EmbeddingManager(_base_cfg(
            llamafile_path=str(binp), mode="cpu",
            extra_args=["--verbose", "--no-warmup"],
        ))
        cmd = m._build_cmd()
        assert cmd[-2:] == ["--verbose", "--no-warmup"]


class TestApeWrap:
    """Linux can't direct-exec APE binaries without binfmt_misc, so
    on POSIX the manager prepends /bin/sh. The wrap must not happen
    on Windows (PE binaries exec normally)."""

    def test_posix_prepends_sh(self):
        with patch.object(em.sys, "platform", "linux"):
            wrapped = em.EmbeddingManager._maybe_wrap_for_ape(
                ["/path/to/llamafile", "--server"]
            )
        assert wrapped == ["/bin/sh", "/path/to/llamafile", "--server"]

    def test_macos_prepends_sh(self):
        with patch.object(em.sys, "platform", "darwin"):
            wrapped = em.EmbeddingManager._maybe_wrap_for_ape(
                ["/path/to/llamafile"]
            )
        assert wrapped == ["/bin/sh", "/path/to/llamafile"]

    def test_windows_no_wrap(self):
        with patch.object(em.sys, "platform", "win32"):
            cmd = ["C:\\path\\llamafile.exe", "--server"]
            wrapped = em.EmbeddingManager._maybe_wrap_for_ape(cmd)
        assert wrapped == cmd

    def test_spawn_uses_wrapped_cmd_on_posix(self, tmp_path):
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"")
        m = em.EmbeddingManager(_base_cfg(llamafile_path=str(binp), mode="cpu"))
        captured = {}

        def _spy(args, **kw):
            captured["args"] = list(args)
            return FakeProc()

        with patch.object(em.sys, "platform", "linux"):
            with patch.object(em.subprocess, "Popen", side_effect=_spy):
                m._spawn_once(cwd=None)
        assert captured["args"][0] == "/bin/sh"
        assert captured["args"][1] == str(binp)


class TestSpawnDetachment:
    """Regression: on Windows the llamafile child must not allocate
    or inherit a visible console. The fix is
    ``creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS``, matching
    the pattern in ``claudemem_reindex._spawn_reindex`` and
    ``lsp_engine.client``. On POSIX we keep ``start_new_session=True``
    so the child outlives the daemon session.
    """

    def _spy_popen(self, em_mod, captured):
        def _spy(args, **kw):
            captured["args"] = list(args)
            captured["kwargs"] = dict(kw)
            return FakeProc()
        return _spy

    def test_windows_passes_no_window_and_detached(self, tmp_path):
        binp = tmp_path / "llamafile.exe"
        binp.write_bytes(b"")
        m = em.EmbeddingManager(_base_cfg(llamafile_path=str(binp), mode="cpu"))
        captured: dict = {}
        # Make the CREATE_NO_WINDOW / DETACHED_PROCESS attributes
        # exist regardless of the host OS — subprocess only defines
        # them on Windows, but the manager references them through
        # the ``subprocess`` module so we can pin them here.
        win_flags = {
            "CREATE_NO_WINDOW": 0x08000000,
            "DETACHED_PROCESS": 0x00000008,
        }
        with patch.object(em.sys, "platform", "win32"):
            with patch.multiple(em.subprocess, **win_flags, create=True):
                with patch.object(em.subprocess, "Popen",
                                  side_effect=self._spy_popen(em, captured)):
                    m._spawn_once(cwd=None)
        kw = captured["kwargs"]
        assert "creationflags" in kw, (
            "Windows spawn must set creationflags to hide the console"
        )
        expected = win_flags["CREATE_NO_WINDOW"] | win_flags["DETACHED_PROCESS"]
        assert kw["creationflags"] == expected
        # start_new_session is a POSIX knob; passing it alongside the
        # Windows creationflags is harmless but we keep them mutually
        # exclusive for clarity.
        assert "start_new_session" not in kw
        # Stdin must be DEVNULL so the child cannot pin a console alive.
        assert kw.get("stdin") is em.subprocess.DEVNULL

    def test_posix_passes_start_new_session(self, tmp_path):
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"")
        m = em.EmbeddingManager(_base_cfg(llamafile_path=str(binp), mode="cpu"))
        captured: dict = {}
        with patch.object(em.sys, "platform", "linux"):
            with patch.object(em.subprocess, "Popen",
                              side_effect=self._spy_popen(em, captured)):
                m._spawn_once(cwd=None)
        kw = captured["kwargs"]
        assert kw.get("start_new_session") is True
        assert "creationflags" not in kw
        assert kw.get("stdin") is em.subprocess.DEVNULL


# --------------------------------------------------------------------- #
# ensure_running / spawn lifecycle
# --------------------------------------------------------------------- #

@pytest.fixture
def writable_binary(tmp_path):
    p = tmp_path / "llamafile"
    p.write_bytes(b"#!/bin/true\n")
    return str(p)


class TestEnsureRunning:
    def test_already_alive_no_spawn(self, writable_binary):
        m = em.EmbeddingManager(_base_cfg(llamafile_path=writable_binary,
                                          mode="cpu"))
        m.proc = FakeProc()  # pretend already up
        with patch.object(em.subprocess, "Popen") as popen_spy:
            result = m.ensure_running()
        popen_spy.assert_not_called()
        assert result == {"ready": True, "port": 38199, "mode": "cpu",
                          "spawned": False}

    def test_cold_spawn_succeeds(self, writable_binary):
        m = em.EmbeddingManager(_base_cfg(llamafile_path=writable_binary,
                                          mode="cpu"))
        proc = FakeProc(pid=99999)
        with patch.object(em.subprocess, "Popen", return_value=proc):
            with patch.object(m, "_wait_for_health", return_value=True):
                with patch.object(m, "_port_free", return_value=True):
                    result = m.ensure_running()
        assert result["ready"] is True
        assert result["spawned"] is True
        assert result["mode"] == "cpu"
        assert m.proc is proc

    def test_spawn_oserror_wrapped(self, writable_binary):
        m = em.EmbeddingManager(_base_cfg(llamafile_path=writable_binary,
                                          mode="cpu"))
        with patch.object(em.subprocess, "Popen",
                          side_effect=OSError("permission")):
            with patch.object(m, "_port_free", return_value=True):
                with pytest.raises(RuntimeError, match="spawn failed"):
                    m.ensure_running()
        assert m.proc is None

    def test_port_busy_refuses(self, writable_binary):
        m = em.EmbeddingManager(_base_cfg(llamafile_path=writable_binary,
                                          mode="cpu"))
        with patch.object(em.subprocess, "Popen") as popen_spy:
            with patch.object(m, "_port_free", return_value=False):
                with pytest.raises(RuntimeError, match="already in"):
                    m.ensure_running()
        popen_spy.assert_not_called()

    def test_gpu_health_fail_falls_back_to_cpu(self, writable_binary):
        """auto mode: first spawn (GPU) doesn't come up healthy →
        terminate, respawn CPU, succeed. gpu_offload_failed sticks."""
        m = em.EmbeddingManager(_base_cfg(llamafile_path=writable_binary,
                                          mode="auto"))
        proc1 = FakeProc(pid=1)
        proc2 = FakeProc(pid=2)
        popen_outputs = [proc1, proc2]
        health_outputs = [False, True]

        def _popen(*a, **kw):
            return popen_outputs.pop(0)

        def _health():
            return health_outputs.pop(0)

        from claude_hooks import gpu_probe
        with patch.object(em.subprocess, "Popen", side_effect=_popen):
            with patch.object(m, "_wait_for_health", side_effect=_health):
                with patch.object(m, "_port_free", return_value=True):
                    with patch.object(gpu_probe, "can_fit_in_vram",
                                      return_value=True):
                        result = m.ensure_running()
        assert result["ready"] is True
        assert result["mode"] == "cpu"
        assert result.get("gpu_fallback") is True
        assert m.gpu_offload_failed is True
        # proc1 was terminated; proc2 is alive.
        assert proc1.terminate_calls >= 1
        assert m.proc is proc2

    def test_cpu_health_fail_no_fallback_raises(self, writable_binary):
        """mode=cpu and health fails → no fallback, raise."""
        m = em.EmbeddingManager(_base_cfg(llamafile_path=writable_binary,
                                          mode="cpu"))
        proc = FakeProc()
        with patch.object(em.subprocess, "Popen", return_value=proc):
            with patch.object(m, "_wait_for_health", return_value=False):
                with patch.object(m, "_port_free", return_value=True):
                    with pytest.raises(RuntimeError, match="did not respond"):
                        m.ensure_running()
        assert m.proc is None
        assert proc.terminate_calls >= 1


# --------------------------------------------------------------------- #
# Idle reaper
# --------------------------------------------------------------------- #

class TestMaybeReap:
    def test_no_proc_no_reap(self):
        m = em.EmbeddingManager(_base_cfg())
        assert m.maybe_reap() is False

    def test_recent_activity_no_reap(self):
        m = em.EmbeddingManager(_base_cfg(idle_timeout_seconds=300.0))
        m.proc = FakeProc()
        m.last_activity_at = time.time()  # just touched
        assert m.maybe_reap() is False
        # proc still alive (not terminated)
        assert m.proc is not None

    def test_idle_reap(self):
        m = em.EmbeddingManager(_base_cfg(idle_timeout_seconds=1.0))
        proc = FakeProc()
        m.proc = proc
        m.last_activity_at = time.time() - 5.0  # well past idle threshold
        assert m.maybe_reap() is True
        assert proc.terminate_calls >= 1
        assert m.proc is None

    def test_terminate_then_sigkill_on_grace_timeout(self):
        """Process ignores SIGTERM → wait raises TimeoutExpired → SIGKILL."""

        class StubbornProc(FakeProc):
            def terminate(self):
                # Pretend SIGTERM doesn't actually exit the process.
                self.terminate_calls += 1
                # returncode stays None — so wait() raises Timeout.

        m = em.EmbeddingManager(_base_cfg())
        stub = StubbornProc()
        m.proc = stub
        m._terminate_locked(grace_seconds=0.01)
        assert stub.terminate_calls == 1
        assert stub.kill_calls == 1
        assert m.proc is None

    def test_shutdown_idempotent(self):
        m = em.EmbeddingManager(_base_cfg())
        m.shutdown()
        m.shutdown()  # no raise


class TestStatus:
    def test_status_alive(self):
        m = em.EmbeddingManager(_base_cfg(mode="cpu"))
        m.proc = FakeProc(pid=4242)
        st = m.status()
        assert st["alive"] is True
        assert st["pid"] == 4242
        assert st["port"] == m.cfg.port
        assert st["mode"] == "cpu"
        assert st["idle_timeout_seconds"] == m.cfg.idle_timeout_seconds

    def test_status_dead(self):
        m = em.EmbeddingManager(_base_cfg())
        st = m.status()
        assert st["alive"] is False
        assert st["pid"] is None
        assert st["port"] is None

    def test_status_reflects_gpu_fallback(self):
        m = em.EmbeddingManager(_base_cfg(mode="auto"))
        m.gpu_offload_failed = True
        m.proc = FakeProc()
        st = m.status()
        assert st["mode"] == "cpu"
        assert st["gpu_offload_failed"] is True


# --------------------------------------------------------------------- #
# _wait_for_health timing
# --------------------------------------------------------------------- #

class TestWaitForHealth:
    def test_returns_true_on_200(self):
        m = em.EmbeddingManager(_base_cfg(spawn_timeout_seconds=2.0))
        with patch("urllib.request.urlopen", return_value=FakeHealthResp(200)):
            assert m._wait_for_health() is True

    def test_returns_false_on_persistent_failure(self):
        import urllib.error
        m = em.EmbeddingManager(_base_cfg(spawn_timeout_seconds=0.5))
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("refused")):
            assert m._wait_for_health() is False

    def test_returns_false_on_oserror(self):
        m = em.EmbeddingManager(_base_cfg(spawn_timeout_seconds=0.5))
        with patch("urllib.request.urlopen", side_effect=OSError("network")):
            assert m._wait_for_health() is False


# --------------------------------------------------------------------- #
# Reaper thread
# --------------------------------------------------------------------- #

class TestReaperThread:
    def test_start_reaper_runs_maybe_reap(self):
        """Thread fires maybe_reap at least once; stops when shutdown set."""
        m = em.EmbeddingManager(_base_cfg(reaper_interval_seconds=0.05,
                                          idle_timeout_seconds=0.01))
        proc = FakeProc()
        m.proc = proc
        m.last_activity_at = time.time() - 1.0  # idle

        m.start_reaper()
        time.sleep(0.8)  # let the reaper cycle a few times
        m.shutdown()

        assert proc.terminate_calls >= 1
        # Reaper thread should have exited.
        if m._reaper_thread is not None:
            m._reaper_thread.join(timeout=2.0)
            assert not m._reaper_thread.is_alive()

    def test_start_reaper_idempotent(self):
        m = em.EmbeddingManager(_base_cfg())
        m.start_reaper()
        first = m._reaper_thread
        m.start_reaper()
        assert m._reaper_thread is first
        m.shutdown()


# --------------------------------------------------------------------- #
# alive() introspection
# --------------------------------------------------------------------- #

class TestAlive:
    def test_alive_no_proc(self):
        m = em.EmbeddingManager(_base_cfg())
        assert m.alive() is False

    def test_alive_running(self):
        m = em.EmbeddingManager(_base_cfg())
        m.proc = FakeProc()
        assert m.alive() is True

    def test_alive_clears_on_exited_proc(self):
        m = em.EmbeddingManager(_base_cfg())
        proc = FakeProc()
        proc.returncode = 1
        m.proc = proc
        assert m.alive() is False
        assert m.proc is None
