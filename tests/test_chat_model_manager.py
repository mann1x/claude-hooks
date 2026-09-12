"""Tests for claude_hooks.chat_model_manager (v1.5).

Mirrors the test_embedding_manager.py mock shape:

- ``FakeProc`` stubs ``subprocess.Popen`` enough to drive the
  ``alive() → terminate() → poll()`` state machine.
- ``FakeHealthResp`` stubs the urllib ``/health`` probe.
- ``gpu_probe.can_fit_in_vram`` is patched to deterministic values.

No real llamafile is launched; the test covers the manager's state
machine across multiple labels, LRU eviction, per-label idle reap,
GPU fallback scoped per-label, and Windows detach kwargs.
"""

from __future__ import annotations

import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import chat_model_manager as cmm  # noqa: E402
from claude_hooks import chat_model_registry as cmr  # noqa: E402


# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #

class FakeProc:
    """Minimal Popen-shape stub. Stays alive until .terminate() /
    .kill() is called, then returns the configured rc."""

    def __init__(self, pid: int = 12345, terminate_rc: int = 0):
        self.pid = pid
        self.returncode = None
        self._terminate_rc = terminate_rc
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = self._terminate_rc

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("llamafile", timeout or 0)
        return self.returncode


class FakeHealthResp:
    def __init__(self, status: int = 200):
        self.status = status
        self._buf = BytesIO(b'{"status":"ok"}')

    def read(self, *a, **kw):
        return self._buf.read(*a, **kw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._buf.close()


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #

GGUF_MAGIC = b"GGUF" + b"\x00" * 60


def _make_gguf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(GGUF_MAGIC)
    return path


@pytest.fixture
def llamafile_bin(tmp_path: Path) -> Path:
    """Fake slim binary — file exists + executable bit set so the
    isfile check passes."""
    p = tmp_path / "llamafile-bin"
    p.write_bytes(b"#!/bin/true\n")
    p.chmod(0o755)
    return p


@pytest.fixture(autouse=True)
def _no_real_gpu_probe():
    """Force gpu_probe.can_fit_in_vram to return False everywhere.

    The manager's lazy ``from claude_hooks import gpu_probe`` inside
    ``_gpu_flags`` is what makes this isolation necessary: without
    the patch, ``gpu_probe.can_fit_in_vram`` shells out to
    ``nvidia-smi`` / ``vulkaninfo`` via ``subprocess.run`` (which
    routes through ``subprocess.Popen``). Any test that mocks
    ``cmm.subprocess.Popen`` with a ``side_effect`` iterator would
    have those probe calls consume entries from the iterator before
    the real spawn even happens. Returning False here short-circuits
    the probe (``--gpu disable`` is appended without any
    subprocess.run), so the only ``Popen`` calls in the tested code
    paths are the ones the test is scripting.

    Individual tests that want to assert the ``-ngl 99`` path patch
    ``can_fit_in_vram`` to True inside the test body, overriding
    this fixture.
    """
    with patch("claude_hooks.gpu_probe.can_fit_in_vram",
               return_value=False):
        yield


@pytest.fixture
def registry(tmp_path: Path) -> cmr.Registry:
    return cmr.Registry(path=tmp_path / "llamafile-models.json")


@pytest.fixture
def two_labels(registry: cmr.Registry, tmp_path: Path) -> dict:
    """Register two labels backed by two GGUFs on different ports."""
    g1 = _make_gguf(tmp_path / "models" / "a.gguf")
    g2 = _make_gguf(tmp_path / "models" / "b.gguf")
    registry.add("alpha", str(g1), ctx_size=4096, port=38093,
                 idle_timeout_seconds=600)
    registry.add("beta", str(g2), ctx_size=4096, port=38094,
                 idle_timeout_seconds=600)
    return {"alpha": g1, "beta": g2}


def _make_manager(llamafile_bin: Path, registry: cmr.Registry,
                  **cfg_overrides) -> cmm.ChatModelManager:
    cfg = cmm.ChatModelsConfig(
        llamafile_path=str(llamafile_bin),
        max_concurrent_loaded=cfg_overrides.pop("max_concurrent_loaded", 2),
        spawn_timeout_seconds=cfg_overrides.pop("spawn_timeout_seconds", 5.0),
        registry_path=registry.path,
        **cfg_overrides,
    )
    return cmm.ChatModelManager(cfg, registry=registry)


# --------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------- #

class TestConfig:
    def test_config_from_dict_defaults(self):
        c = cmm.config_from_dict({})
        assert c.max_concurrent_loaded == 2
        assert c.host == "127.0.0.1"
        assert c.llamafile_path == ""

    def test_config_from_dict_overrides(self):
        c = cmm.config_from_dict({"chat_models": {
            "llamafile_path": "/bin/llamafile",
            "max_concurrent_loaded": 4,
            "vram_budget_mb": 8192,
        }})
        assert c.llamafile_path == "/bin/llamafile"
        assert c.max_concurrent_loaded == 4
        assert c.vram_budget_mb == 8192


# --------------------------------------------------------------------- #
# Command building
# --------------------------------------------------------------------- #

class TestBuildCmd:
    def test_cmd_drops_embedding_flags(self, llamafile_bin, registry,
                                       two_labels):
        m = _make_manager(llamafile_bin, registry)
        spec = registry.get("alpha")
        with patch("claude_hooks.gpu_probe.can_fit_in_vram",
                   return_value=True):
            cmd = m._build_cmd(spec)
        # No --embedding flag — that's embedding-server mode.
        assert "--embedding" not in cmd
        # No --pooling flag either.
        assert "--pooling" not in cmd
        # But the canonical flags are present.
        assert "--server" in cmd
        assert "--port" in cmd and "38093" in cmd
        assert "--ctx-size" in cmd and "4096" in cmd
        # And -m points at the GGUF.
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == str(two_labels["alpha"])

    def test_cmd_includes_ngl_when_gpu(self, llamafile_bin, registry,
                                       two_labels):
        m = _make_manager(llamafile_bin, registry)
        spec = registry.get("alpha")
        with patch("claude_hooks.gpu_probe.can_fit_in_vram",
                   return_value=True):
            cmd = m._build_cmd(spec)
        assert "-ngl" in cmd and "99" in cmd

    def test_cmd_passes_gpu_disable_when_cpu(self, llamafile_bin,
                                             registry, two_labels):
        m = _make_manager(llamafile_bin, registry)
        spec = registry.get("alpha")
        spec.mode = "cpu"
        cmd = m._build_cmd(spec)
        assert "--gpu" in cmd
        assert "disable" in cmd
        assert "-ngl" not in cmd

    def test_cmd_passes_gpu_disable_when_vram_insufficient(
        self, llamafile_bin, registry, two_labels
    ):
        m = _make_manager(llamafile_bin, registry)
        spec = registry.get("alpha")
        with patch("claude_hooks.gpu_probe.can_fit_in_vram",
                   return_value=False):
            cmd = m._build_cmd(spec)
        assert "--gpu" in cmd and "disable" in cmd

    def test_cmd_missing_llamafile_path_raises(self, registry, two_labels):
        cfg = cmm.ChatModelsConfig(llamafile_path="",
                                   registry_path=registry.path)
        m = cmm.ChatModelManager(cfg, registry=registry)
        spec = registry.get("alpha")
        with pytest.raises(RuntimeError, match="llamafile_path"):
            m._build_cmd(spec)

    def test_cmd_missing_gguf_raises(self, llamafile_bin, registry,
                                     two_labels, tmp_path):
        m = _make_manager(llamafile_bin, registry)
        # Delete the GGUF after registration.
        Path(two_labels["alpha"]).unlink()
        spec = registry.get("alpha")
        with pytest.raises(RuntimeError, match="GGUF for label"):
            m._build_cmd(spec)


# --------------------------------------------------------------------- #
# APE wrap + Windows detach
# --------------------------------------------------------------------- #

class TestApeWrap:
    def test_posix_prepends_sh(self):
        with patch.object(cmm.sys, "platform", "linux"):
            wrapped = cmm.ChatModelManager._maybe_wrap_for_ape(
                ["/x/llamafile", "--server"]
            )
        assert wrapped == ["/bin/sh", "/x/llamafile", "--server"]

    def test_macos_prepends_sh(self):
        with patch.object(cmm.sys, "platform", "darwin"):
            wrapped = cmm.ChatModelManager._maybe_wrap_for_ape(
                ["/x/llamafile"]
            )
        assert wrapped == ["/bin/sh", "/x/llamafile"]

    def test_windows_no_wrap(self):
        with patch.object(cmm.sys, "platform", "win32"):
            cmd = ["C:\\llamafile.exe", "--server"]
            wrapped = cmm.ChatModelManager._maybe_wrap_for_ape(cmd)
        assert wrapped == cmd


class TestSpawnDetachment:
    """Same as the v1.4 embedding regression — the chat manager must
    use the right detach flags per platform."""

    def _spy_popen(self, captured):
        def _spy(args, **kw):
            captured["args"] = list(args)
            captured["kwargs"] = dict(kw)
            return FakeProc()
        return _spy

    def test_windows_passes_no_window_and_detached(
        self, llamafile_bin, registry, two_labels
    ):
        m = _make_manager(llamafile_bin, registry)
        spec = registry.get("alpha")
        captured: dict = {}
        win_flags = {
            "CREATE_NO_WINDOW": 0x08000000,
            "DETACHED_PROCESS": 0x00000008,
        }
        with patch.object(cmm.sys, "platform", "win32"):
            with patch.multiple(cmm.subprocess, **win_flags, create=True):
                with patch.object(cmm.subprocess, "Popen",
                                  side_effect=self._spy_popen(captured)):
                    with patch("claude_hooks.gpu_probe.can_fit_in_vram",
                               return_value=False):
                        m._spawn_once(spec)
        kw = captured["kwargs"]
        assert "creationflags" in kw
        expected = win_flags["CREATE_NO_WINDOW"] | win_flags["DETACHED_PROCESS"]
        assert kw["creationflags"] == expected
        assert "start_new_session" not in kw
        assert kw.get("stdin") is cmm.subprocess.DEVNULL

    def test_posix_passes_start_new_session(
        self, llamafile_bin, registry, two_labels
    ):
        m = _make_manager(llamafile_bin, registry)
        spec = registry.get("alpha")
        captured: dict = {}
        with patch.object(cmm.sys, "platform", "linux"):
            with patch.object(cmm.subprocess, "Popen",
                              side_effect=self._spy_popen(captured)):
                with patch("claude_hooks.gpu_probe.can_fit_in_vram",
                           return_value=False):
                    m._spawn_once(spec)
        kw = captured["kwargs"]
        assert kw.get("start_new_session") is True
        assert "creationflags" not in kw
        assert kw.get("stdin") is cmm.subprocess.DEVNULL


# --------------------------------------------------------------------- #
# ensure_running — single-label happy paths
# --------------------------------------------------------------------- #

class TestEnsureRunning:
    def test_unknown_label_raises(self, llamafile_bin, registry):
        m = _make_manager(llamafile_bin, registry)
        with pytest.raises(cmr.UnknownLabel):
            m.ensure_running("ghost")

    def test_cold_spawn(self, llamafile_bin, registry, two_labels):
        m = _make_manager(llamafile_bin, registry)
        with patch.object(cmm.subprocess, "Popen",
                          return_value=FakeProc(pid=42)):
            with patch.object(m, "_wait_for_health", return_value=True):
                with patch.object(m, "_port_free", return_value=True):
                    r = m.ensure_running("alpha")
        assert r["ready"] is True
        assert r["spawned"] is True
        assert r["port"] == 38093
        assert r["label"] == "alpha"
        assert r["evicted"] == []
        assert m.alive("alpha")

    def test_already_alive_no_spawn(self, llamafile_bin, registry,
                                    two_labels):
        m = _make_manager(llamafile_bin, registry)
        # Prime with a handle.
        m.handles["alpha"] = cmm.ProcessHandle(
            label="alpha", proc=FakeProc(), port=38093,
            mode="auto", last_activity_at=time.time() - 100,
        )
        with patch.object(cmm.subprocess, "Popen") as popen_spy:
            r = m.ensure_running("alpha")
        popen_spy.assert_not_called()
        assert r["spawned"] is False
        # last_activity_at was touched.
        assert m.handles["alpha"].last_activity_at >= time.time() - 2

    def test_port_busy_refuses(self, llamafile_bin, registry, two_labels):
        m = _make_manager(llamafile_bin, registry)
        with patch.object(cmm.subprocess, "Popen") as popen_spy:
            with patch.object(m, "_port_free", return_value=False):
                with pytest.raises(RuntimeError, match="already in use"):
                    m.ensure_running("alpha")
        popen_spy.assert_not_called()

    def test_spawn_oserror_wrapped(self, llamafile_bin, registry,
                                   two_labels):
        m = _make_manager(llamafile_bin, registry)
        with patch.object(cmm.subprocess, "Popen",
                          side_effect=OSError("perm")):
            with patch.object(m, "_port_free", return_value=True):
                with pytest.raises(RuntimeError, match="spawn failed"):
                    m.ensure_running("alpha")
        assert "alpha" not in m.handles


# --------------------------------------------------------------------- #
# GPU fallback — per-label sticky
# --------------------------------------------------------------------- #

class TestGpuFallback:
    def test_gpu_health_fails_falls_back_to_cpu(
        self, llamafile_bin, registry, two_labels
    ):
        m = _make_manager(llamafile_bin, registry)
        spec = registry.get("alpha")
        spec.mode = "auto"

        call_count = {"n": 0}

        def health_seq(_port):
            call_count["n"] += 1
            return call_count["n"] >= 2  # first try fails, second succeeds

        with patch.object(cmm.subprocess, "Popen",
                          return_value=FakeProc()):
            with patch.object(m, "_port_free", return_value=True):
                with patch.object(m, "_wait_for_health",
                                  side_effect=health_seq):
                    with patch("claude_hooks.gpu_probe.can_fit_in_vram",
                               return_value=True):
                        r = m.ensure_running("alpha")
        assert r["mode"] == "cpu"
        assert r.get("gpu_fallback") is True
        assert "alpha" in m._gpu_failed

    def test_gpu_failure_scoped_per_label(self, llamafile_bin, registry,
                                          two_labels):
        """alpha's GPU failure must not force beta to CPU."""
        m = _make_manager(llamafile_bin, registry)
        m._gpu_failed.add("alpha")
        spec_alpha = registry.get("alpha")
        spec_beta = registry.get("beta")
        assert m._effective_mode(spec_alpha) == "cpu"
        assert m._effective_mode(spec_beta) == "auto"


# --------------------------------------------------------------------- #
# LRU eviction
# --------------------------------------------------------------------- #

class TestLRUEviction:
    def test_evicts_oldest_when_cap_exceeded(
        self, llamafile_bin, registry, tmp_path
    ):
        # Cap=1 means any second ensure evicts the first.
        m = _make_manager(llamafile_bin, registry,
                          max_concurrent_loaded=1)
        g_a = _make_gguf(tmp_path / "ma" / "a.gguf")
        g_b = _make_gguf(tmp_path / "mb" / "b.gguf")
        registry.add("a", str(g_a), port=38093)
        registry.add("b", str(g_b), port=38094)

        # Override the registry envelope's cap so the manager reads 1.
        env = registry.load()
        env["max_concurrent_loaded"] = 1
        registry.save(env)

        proc_a = FakeProc(pid=11)
        proc_b = FakeProc(pid=22)
        popen_seq = iter([proc_a, proc_b])

        with patch.object(cmm.subprocess, "Popen",
                          side_effect=lambda *a, **kw: next(popen_seq)):
            with patch.object(m, "_wait_for_health", return_value=True):
                with patch.object(m, "_port_free", return_value=True):
                    r_a = m.ensure_running("a")
                    # Force a known time delta so a is the older one.
                    m.handles["a"].last_activity_at = time.time() - 100
                    r_b = m.ensure_running("b")
        assert r_a["evicted"] == []
        assert r_b["evicted"] == ["a"]
        # a is no longer in handles; b is alive.
        assert "a" not in m.handles
        assert "b" in m.handles
        # a's process was terminated.
        assert proc_a.terminate_calls == 1

    def test_cap_2_admits_two_then_evicts_on_third(
        self, llamafile_bin, registry, tmp_path
    ):
        m = _make_manager(llamafile_bin, registry,
                          max_concurrent_loaded=2)
        for name, port in (("a", 38093), ("b", 38094), ("c", 38095)):
            g = _make_gguf(tmp_path / "m" / f"{name}.gguf")
            registry.add(name, str(g), port=port)

        procs = [FakeProc(pid=i) for i in (11, 22, 33)]
        proc_iter = iter(procs)

        with patch.object(cmm.subprocess, "Popen",
                          side_effect=lambda *a, **kw: next(proc_iter)):
            with patch.object(m, "_wait_for_health", return_value=True):
                with patch.object(m, "_port_free", return_value=True):
                    r_a = m.ensure_running("a")
                    m.handles["a"].last_activity_at = time.time() - 200
                    r_b = m.ensure_running("b")
                    m.handles["b"].last_activity_at = time.time() - 100
                    r_c = m.ensure_running("c")
        assert r_a["evicted"] == []
        assert r_b["evicted"] == []
        # c evicts a (oldest), not b.
        assert r_c["evicted"] == ["a"]
        assert set(m.handles.keys()) == {"b", "c"}

    def test_eviction_skips_admitting_label(self, llamafile_bin, registry,
                                            tmp_path):
        """If we're re-ensuring the same label that's already loaded,
        we shouldn't accidentally evict it."""
        m = _make_manager(llamafile_bin, registry,
                          max_concurrent_loaded=1)
        g = _make_gguf(tmp_path / "m" / "x.gguf")
        registry.add("x", str(g), port=38093)
        env = registry.load()
        env["max_concurrent_loaded"] = 1
        registry.save(env)
        with patch.object(cmm.subprocess, "Popen", return_value=FakeProc()):
            with patch.object(m, "_wait_for_health", return_value=True):
                with patch.object(m, "_port_free", return_value=True):
                    m.ensure_running("x")
                    # Re-ensure same label → no spawn, no eviction.
                    r = m.ensure_running("x")
        assert r["spawned"] is False


# --------------------------------------------------------------------- #
# Idle reaping (per-label timeouts)
# --------------------------------------------------------------------- #

class TestIdleReap:
    def test_reap_when_idle_exceeds_per_label_timeout(
        self, llamafile_bin, registry, two_labels
    ):
        # alpha has idle_timeout=600. Force it to look old.
        m = _make_manager(llamafile_bin, registry)
        proc = FakeProc(pid=42)
        m.handles["alpha"] = cmm.ProcessHandle(
            label="alpha", proc=proc, port=38093, mode="auto",
            last_activity_at=time.time() - 1200,
        )
        reaped = m.maybe_reap()
        assert reaped == ["alpha"]
        assert "alpha" not in m.handles
        assert proc.terminate_calls == 1

    def test_reap_skips_fresh_handle(self, llamafile_bin, registry,
                                     two_labels):
        m = _make_manager(llamafile_bin, registry)
        m.handles["alpha"] = cmm.ProcessHandle(
            label="alpha", proc=FakeProc(), port=38093, mode="auto",
            last_activity_at=time.time() - 10,  # very fresh
        )
        reaped = m.maybe_reap()
        assert reaped == []
        assert "alpha" in m.handles

    def test_reap_uses_per_label_timeout(self, llamafile_bin, registry,
                                         tmp_path):
        """alpha has 30s timeout, beta has 1800s — beta survives."""
        g_a = _make_gguf(tmp_path / "ma" / "a.gguf")
        g_b = _make_gguf(tmp_path / "mb" / "b.gguf")
        registry.add("alpha", str(g_a), port=38093,
                     idle_timeout_seconds=30)
        registry.add("beta", str(g_b), port=38094,
                     idle_timeout_seconds=1800)
        m = _make_manager(llamafile_bin, registry)
        m.handles["alpha"] = cmm.ProcessHandle(
            label="alpha", proc=FakeProc(pid=1), port=38093, mode="auto",
            last_activity_at=time.time() - 60,
        )
        m.handles["beta"] = cmm.ProcessHandle(
            label="beta", proc=FakeProc(pid=2), port=38094, mode="auto",
            last_activity_at=time.time() - 60,
        )
        reaped = m.maybe_reap()
        assert reaped == ["alpha"]
        assert "beta" in m.handles

    def test_reap_orphan_label(self, llamafile_bin, registry, two_labels):
        """Label removed from registry while alive → reaper reaps it."""
        m = _make_manager(llamafile_bin, registry)
        m.handles["alpha"] = cmm.ProcessHandle(
            label="alpha", proc=FakeProc(), port=38093, mode="auto",
            last_activity_at=time.time(),  # fresh!
        )
        registry.remove("alpha")
        reaped = m.maybe_reap()
        assert reaped == ["alpha"]


# --------------------------------------------------------------------- #
# Status + shutdown + gc
# --------------------------------------------------------------------- #

class TestStatus:
    def test_status_all_with_no_handles(self, llamafile_bin, registry,
                                        two_labels):
        m = _make_manager(llamafile_bin, registry)
        s = m.status()
        labels = [r["label"] for r in s["models"]]
        assert set(labels) == {"alpha", "beta"}
        for row in s["models"]:
            assert row["alive"] is False

    def test_status_one_alive(self, llamafile_bin, registry, two_labels):
        m = _make_manager(llamafile_bin, registry)
        m.handles["alpha"] = cmm.ProcessHandle(
            label="alpha", proc=FakeProc(pid=42), port=38093, mode="auto",
            last_activity_at=time.time(),
        )
        s = m.status()
        rows = {r["label"]: r for r in s["models"]}
        assert rows["alpha"]["alive"] is True
        assert rows["alpha"]["pid"] == 42
        assert rows["beta"]["alive"] is False
        assert s["loaded"] == 1

    def test_status_label_filter(self, llamafile_bin, registry,
                                 two_labels):
        m = _make_manager(llamafile_bin, registry)
        s = m.status("alpha")
        assert len(s["models"]) == 1
        assert s["models"][0]["label"] == "alpha"

    def test_orphan_handle_surfaces(self, llamafile_bin, registry,
                                    two_labels):
        m = _make_manager(llamafile_bin, registry)
        m.handles["alpha"] = cmm.ProcessHandle(
            label="alpha", proc=FakeProc(), port=38093, mode="auto",
            last_activity_at=time.time(),
        )
        registry.remove("alpha")
        s = m.status()
        # alpha is no longer registered but still loaded → orphan row.
        orphans = [r for r in s["models"] if r["label"] == "alpha"]
        assert len(orphans) == 1
        assert orphans[0]["orphan"] is True


class TestShutdown:
    def test_shutdown_one(self, llamafile_bin, registry, two_labels):
        m = _make_manager(llamafile_bin, registry)
        proc = FakeProc(pid=10)
        m.handles["alpha"] = cmm.ProcessHandle(
            label="alpha", proc=proc, port=38093, mode="auto",
            last_activity_at=time.time(),
        )
        r = m.shutdown("alpha")
        assert r["stopped"] is True
        assert "alpha" not in m.handles
        assert proc.terminate_calls == 1

    def test_shutdown_one_not_loaded(self, llamafile_bin, registry,
                                     two_labels):
        m = _make_manager(llamafile_bin, registry)
        r = m.shutdown("alpha")
        assert r["stopped"] is False

    def test_shutdown_all_reaps_everything(self, llamafile_bin, registry,
                                           two_labels):
        m = _make_manager(llamafile_bin, registry)
        p1 = FakeProc(pid=1)
        p2 = FakeProc(pid=2)
        m.handles["alpha"] = cmm.ProcessHandle(
            label="alpha", proc=p1, port=38093, mode="auto",
            last_activity_at=time.time(),
        )
        m.handles["beta"] = cmm.ProcessHandle(
            label="beta", proc=p2, port=38094, mode="auto",
            last_activity_at=time.time(),
        )
        r = m.shutdown()
        assert r["stopped"] is True
        assert m.handles == {}
        assert p1.terminate_calls == 1
        assert p2.terminate_calls == 1


class TestGc:
    def test_gc_reaps_orphan_labels(self, llamafile_bin, registry,
                                    two_labels):
        m = _make_manager(llamafile_bin, registry)
        m.handles["alpha"] = cmm.ProcessHandle(
            label="alpha", proc=FakeProc(), port=38093, mode="auto",
            last_activity_at=time.time(),
        )
        m.handles["beta"] = cmm.ProcessHandle(
            label="beta", proc=FakeProc(), port=38094, mode="auto",
            last_activity_at=time.time(),
        )
        registry.remove("alpha")
        r = m.gc()
        assert r["evicted"] == ["alpha"]
        assert "alpha" not in m.handles
        assert "beta" in m.handles


# --------------------------------------------------------------------- #
# Registry mtime reload
# --------------------------------------------------------------------- #

class TestRegistryReload:
    def test_reload_picks_up_new_label(self, llamafile_bin, registry,
                                       tmp_path):
        """Add label to registry after manager construction, then
        ensure_running picks it up."""
        m = _make_manager(llamafile_bin, registry)
        g = _make_gguf(tmp_path / "m" / "new.gguf")
        # Bump mtime by sleeping then writing.
        time.sleep(0.01)
        registry.add("late", str(g), port=38093)
        with patch.object(cmm.subprocess, "Popen", return_value=FakeProc()):
            with patch.object(m, "_wait_for_health", return_value=True):
                with patch.object(m, "_port_free", return_value=True):
                    r = m.ensure_running("late")
        assert r["ready"] is True
