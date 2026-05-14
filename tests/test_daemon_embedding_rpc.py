"""Tests for the v1.4 embedding RPC ops in claude_hooks.daemon
+ the typed wrappers in claude_hooks.daemon_client.

The daemon's TCP server is exercised end-to-end (real socket, real
HMAC) but the EmbeddingManager itself is stubbed — these tests prove
the wire protocol & dispatch routing, not the lifecycle state machine
(that's tests/test_embedding_manager.py).
"""

from __future__ import annotations

import os
import threading
from unittest.mock import patch

import pytest

from claude_hooks import daemon, daemon_client


# --------------------------------------------------------------------- #
# _build_embedding_manager — config-block parsing
# --------------------------------------------------------------------- #

class TestBuildEmbeddingManager:
    def test_no_embedding_block_returns_none(self):
        assert daemon._build_embedding_manager({}) is None

    def test_disabled_returns_none(self):
        assert daemon._build_embedding_manager(
            {"embedding": {"enabled": False, "llamafile_path": "/x"}}
        ) is None

    def test_enabled_builds_manager(self, tmp_path):
        # Need a real path for the config_from_dict happy path; the
        # manager itself doesn't spawn at construction so a touch file
        # is enough.
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"")
        cfg = {"embedding": {
            "enabled": True,
            "llamafile_path": str(binp),
            "port": 38192,
            "mode": "cpu",
        }}
        mgr = daemon._build_embedding_manager(cfg)
        assert mgr is not None
        assert mgr.cfg.port == 38192
        assert mgr.cfg.mode == "cpu"

    def test_bad_config_returns_none(self):
        """Invalid mode would raise ValueError in EmbeddingManager
        __init__; _build_embedding_manager should swallow it and
        return None so the daemon still starts."""
        cfg = {"embedding": {
            "enabled": True,
            "llamafile_path": "/x",
            "mode": "🚀",
        }}
        assert daemon._build_embedding_manager(cfg) is None

    def test_non_dict_embedding_returns_none(self):
        assert daemon._build_embedding_manager(
            {"embedding": "not a dict"}
        ) is None

    def test_non_dict_cfg_returns_none(self):
        assert daemon._build_embedding_manager("oops") is None


# --------------------------------------------------------------------- #
# Stub manager + running daemon fixture
# --------------------------------------------------------------------- #

class _StubManager:
    """Behaves like EmbeddingManager for the three RPC entry points."""

    def __init__(self, *, ensure=None, status=None,
                 ensure_raises=None, shutdown_raises=None):
        self._ensure = ensure
        self._status = status
        self._ensure_raises = ensure_raises
        self._shutdown_raises = shutdown_raises
        self.ensure_calls = 0
        self.status_calls = 0
        self.shutdown_calls = 0

    def ensure_running(self):
        self.ensure_calls += 1
        if self._ensure_raises:
            raise self._ensure_raises
        return self._ensure or {"ready": True, "port": 38092, "mode": "cpu"}

    def status(self):
        self.status_calls += 1
        return self._status or {"alive": True, "port": 38092}

    def shutdown(self):
        self.shutdown_calls += 1
        if self._shutdown_raises:
            raise self._shutdown_raises


@pytest.fixture
def running_daemon_with_manager(tmp_path, request):
    """Spin a daemon with the test-provided embedding_manager attached."""
    mgr = getattr(request, "param", None)
    secret_path = tmp_path / "secret"
    daemon.ensure_secret(secret_path)
    secret = secret_path.read_text(encoding="utf-8").strip()
    srv = daemon.DaemonServer(
        "127.0.0.1", 0, secret=secret, embedding_manager=mgr,
    )
    host, port = srv.server_address
    t = threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    t.start()
    try:
        yield host, port, secret_path, mgr
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------- #
# RPC end-to-end
# --------------------------------------------------------------------- #

class TestEmbeddingRpcMissingManager:
    """When no embedding manager is attached, the daemon responds with
    a structured ``available: false`` instead of crashing."""

    @pytest.mark.parametrize("running_daemon_with_manager", [None], indirect=True)
    def test_ensure_returns_available_false(self, running_daemon_with_manager):
        host, port, secret_path, _ = running_daemon_with_manager
        out = daemon_client.embedding_ensure(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out == {"available": False,
                       "reason": "embedding manager not configured"}

    @pytest.mark.parametrize("running_daemon_with_manager", [None], indirect=True)
    def test_status_returns_available_false(self, running_daemon_with_manager):
        host, port, secret_path, _ = running_daemon_with_manager
        out = daemon_client.embedding_status(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out["available"] is False

    @pytest.mark.parametrize("running_daemon_with_manager", [None], indirect=True)
    def test_shutdown_returns_false(self, running_daemon_with_manager):
        host, port, secret_path, _ = running_daemon_with_manager
        out = daemon_client.embedding_shutdown(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        # No manager → handler returns ``available: false`` (not the
        # ``shutdown: true`` shape the wrapper looks for) → wrapper
        # returns False.
        assert out is False


class TestEmbeddingRpcWithManager:
    @pytest.mark.parametrize(
        "running_daemon_with_manager", [_StubManager()], indirect=True,
    )
    def test_ensure_invokes_manager(self, running_daemon_with_manager):
        host, port, secret_path, mgr = running_daemon_with_manager
        out = daemon_client.embedding_ensure(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out == {"ready": True, "port": 38092, "mode": "cpu"}
        assert mgr.ensure_calls == 1

    @pytest.mark.parametrize(
        "running_daemon_with_manager", [_StubManager()], indirect=True,
    )
    def test_status_invokes_manager(self, running_daemon_with_manager):
        host, port, secret_path, mgr = running_daemon_with_manager
        out = daemon_client.embedding_status(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out == {"alive": True, "port": 38092}
        assert mgr.status_calls == 1

    @pytest.mark.parametrize(
        "running_daemon_with_manager", [_StubManager()], indirect=True,
    )
    def test_shutdown_invokes_manager(self, running_daemon_with_manager):
        host, port, secret_path, mgr = running_daemon_with_manager
        out = daemon_client.embedding_shutdown(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out is True
        assert mgr.shutdown_calls == 1

    @pytest.mark.parametrize(
        "running_daemon_with_manager",
        [_StubManager(ensure_raises=RuntimeError("port 38092 busy"))],
        indirect=True,
    )
    def test_ensure_runtime_error_returned_as_unavailable(
        self, running_daemon_with_manager,
    ):
        host, port, secret_path, mgr = running_daemon_with_manager
        out = daemon_client.embedding_ensure(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out == {"available": False, "reason": "port 38092 busy"}
        assert mgr.ensure_calls == 1


class TestRequestShutdownReapsManager:
    """When the daemon receives ``_shutdown``, it should reap the
    embedding manager before tearing down the server loop. Verified
    on the DaemonServer object directly — we don't actually need a
    live socket for this."""

    def test_request_shutdown_reaps_manager(self):
        secret = "preset-secret-for-test"
        mgr = _StubManager()
        srv = daemon.DaemonServer(
            "127.0.0.1", 0, secret=secret, embedding_manager=mgr,
        )
        try:
            srv.request_shutdown()
            # Give the side thread a moment to fire shutdown.
            import time as _t
            for _ in range(20):
                if srv.stop_event.is_set():
                    break
                _t.sleep(0.05)
        finally:
            srv.server_close()
        assert mgr.shutdown_calls == 1

    def test_request_shutdown_no_manager(self):
        srv = daemon.DaemonServer(
            "127.0.0.1", 0, secret="s", embedding_manager=None,
        )
        try:
            # Must not raise even though there's no manager.
            srv.request_shutdown()
        finally:
            srv.server_close()


# --------------------------------------------------------------------- #
# Client wrappers — unit tests with mocked ``call``
# --------------------------------------------------------------------- #

class TestClientWrappersMocked:
    def test_embedding_ensure_returns_none_when_daemon_down(self):
        with patch.object(daemon_client, "call", return_value=None):
            assert daemon_client.embedding_ensure() is None

    def test_embedding_ensure_returns_result_on_ok(self):
        with patch.object(daemon_client, "call",
                          return_value={"ok": True, "result": {
                              "ready": True, "port": 38092, "mode": "cpu"}}):
            out = daemon_client.embedding_ensure()
        assert out == {"ready": True, "port": 38092, "mode": "cpu"}

    def test_embedding_ensure_returns_unavailable_on_nack(self):
        with patch.object(daemon_client, "call",
                          return_value={"ok": False, "error": "auth"}):
            out = daemon_client.embedding_ensure()
        assert out == {"available": False, "reason": "auth"}

    def test_embedding_status_returns_none_when_daemon_down(self):
        with patch.object(daemon_client, "call", return_value=None):
            assert daemon_client.embedding_status() is None

    def test_embedding_status_returns_result_on_ok(self):
        with patch.object(daemon_client, "call",
                          return_value={"ok": True, "result": {
                              "alive": True, "port": 38092,
                              "idle_seconds": 1.2}}):
            out = daemon_client.embedding_status()
        assert out["alive"] is True
        assert out["port"] == 38092

    def test_embedding_shutdown_true_only_on_confirmation(self):
        with patch.object(daemon_client, "call",
                          return_value={"ok": True,
                                        "result": {"shutdown": True}}):
            assert daemon_client.embedding_shutdown() is True

    def test_embedding_shutdown_false_on_no_confirmation(self):
        with patch.object(daemon_client, "call",
                          return_value={"ok": True, "result": {}}):
            assert daemon_client.embedding_shutdown() is False

    def test_embedding_shutdown_false_on_no_response(self):
        with patch.object(daemon_client, "call", return_value=None):
            assert daemon_client.embedding_shutdown() is False
