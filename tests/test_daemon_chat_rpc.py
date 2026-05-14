"""Tests for the v1.5 chat-model RPC ops in claude_hooks.daemon
+ the typed wrappers in claude_hooks.daemon_client.

The daemon's TCP server is exercised end-to-end (real socket, real
HMAC) but the ChatModelManager itself is stubbed — these tests prove
the wire protocol & dispatch routing, not the lifecycle state
machine (that's tests/test_chat_model_manager.py).

Mirrors the test_daemon_embedding_rpc.py shape, with per-label
routing surfaces and the gc op added.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import daemon, daemon_client  # noqa: E402


# --------------------------------------------------------------------- #
# _build_chat_model_manager — config-block parsing
# --------------------------------------------------------------------- #

class TestBuildChatModelManager:
    def test_no_block_returns_none(self):
        assert daemon._build_chat_model_manager({}) is None

    def test_disabled_returns_none(self):
        assert daemon._build_chat_model_manager(
            {"chat_models": {"enabled": False}}
        ) is None

    def test_enabled_builds_manager(self, tmp_path):
        # The manager construction doesn't spawn anything; it just
        # holds the config + registry handle, so an empty registry
        # file is enough.
        binp = tmp_path / "llamafile"
        binp.write_bytes(b"")
        reg_path = tmp_path / "models.json"
        cfg = {"chat_models": {
            "enabled": True,
            "llamafile_path": str(binp),
            "max_concurrent_loaded": 3,
            "registry_path": str(reg_path),
        }}
        mgr = daemon._build_chat_model_manager(cfg)
        assert mgr is not None
        assert mgr.cfg.llamafile_path == str(binp)
        assert mgr.cfg.max_concurrent_loaded == 3

    def test_non_dict_block_returns_none(self):
        assert daemon._build_chat_model_manager(
            {"chat_models": "oops"}
        ) is None

    def test_non_dict_cfg_returns_none(self):
        assert daemon._build_chat_model_manager("oops") is None


# --------------------------------------------------------------------- #
# Stub manager + running daemon fixture
# --------------------------------------------------------------------- #

class _StubChatManager:
    """Behaves like ChatModelManager for the four RPC entry points."""

    def __init__(self, *,
                 ensure=None, status=None, shutdown=None, gc_result=None,
                 ensure_raises=None):
        self._ensure = ensure
        self._status = status
        self._shutdown = shutdown
        self._gc_result = gc_result
        self._ensure_raises = ensure_raises
        self.ensure_calls: list = []
        self.status_calls: list = []
        self.shutdown_calls: list = []
        self.gc_calls = 0

    def ensure_running(self, label):
        self.ensure_calls.append(label)
        if self._ensure_raises:
            raise self._ensure_raises
        return self._ensure or {
            "label": label, "ready": True, "port": 38093,
            "mode": "auto", "spawned": True, "evicted": [],
        }

    def status(self, label=None):
        self.status_calls.append(label)
        return self._status or {
            "models": [{"label": "alpha", "alive": True, "port": 38093}],
            "loaded": 1,
            "max_concurrent_loaded": 2,
        }

    def shutdown(self, label=None):
        self.shutdown_calls.append(label)
        return self._shutdown or {"stopped": True, "label": label}

    def gc(self):
        self.gc_calls += 1
        return self._gc_result or {"evicted": []}


@pytest.fixture
def running_daemon_with_chat_mgr(tmp_path, request):
    """Spin a daemon with the test-provided chat_model_manager attached."""
    mgr = getattr(request, "param", None)
    secret_path = tmp_path / "secret"
    daemon.ensure_secret(secret_path)
    secret = secret_path.read_text(encoding="utf-8").strip()
    srv = daemon.DaemonServer(
        "127.0.0.1", 0, secret=secret,
        embedding_manager=None,
        chat_model_manager=mgr,
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
# Missing-manager path — structured available=false, no crash
# --------------------------------------------------------------------- #

class TestChatRpcMissingManager:
    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr", [None], indirect=True,
    )
    def test_ensure_returns_available_false(
        self, running_daemon_with_chat_mgr
    ):
        host, port, secret_path, _ = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_ensure(
            "gemma",
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out == {"available": False,
                       "reason": "chat model manager not configured"}

    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr", [None], indirect=True,
    )
    def test_status_returns_available_false(
        self, running_daemon_with_chat_mgr
    ):
        host, port, secret_path, _ = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_status(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out["available"] is False

    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr", [None], indirect=True,
    )
    def test_shutdown_returns_available_false(
        self, running_daemon_with_chat_mgr
    ):
        host, port, secret_path, _ = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_shutdown(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out["available"] is False

    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr", [None], indirect=True,
    )
    def test_gc_returns_available_false(
        self, running_daemon_with_chat_mgr
    ):
        host, port, secret_path, _ = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_gc(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out["available"] is False


# --------------------------------------------------------------------- #
# With-manager path — RPC reaches the stub
# --------------------------------------------------------------------- #

class TestChatRpcWithManager:
    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr", [_StubChatManager()], indirect=True,
    )
    def test_ensure_passes_label(self, running_daemon_with_chat_mgr):
        host, port, secret_path, mgr = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_ensure(
            "gemma4-e4b",
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out["ready"] is True
        assert out["label"] == "gemma4-e4b"
        assert mgr.ensure_calls == ["gemma4-e4b"]

    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr", [_StubChatManager()], indirect=True,
    )
    def test_ensure_missing_label_returns_unavailable(
        self, running_daemon_with_chat_mgr
    ):
        host, port, secret_path, mgr = running_daemon_with_chat_mgr
        # Daemon checks label presence before invoking the manager.
        # Send a request with no label by calling the raw wire.
        resp = daemon_client.call(
            "_chat_model_ensure", {},
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert resp["ok"] is True  # the protocol envelope is ok
        assert resp["result"]["available"] is False
        assert "label is required" in resp["result"]["reason"]
        assert mgr.ensure_calls == []  # never reached the manager

    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr", [_StubChatManager()], indirect=True,
    )
    def test_status_no_label(self, running_daemon_with_chat_mgr):
        host, port, secret_path, mgr = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_status(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out["loaded"] == 1
        assert mgr.status_calls == [None]

    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr", [_StubChatManager()], indirect=True,
    )
    def test_status_with_label(self, running_daemon_with_chat_mgr):
        host, port, secret_path, mgr = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_status(
            "alpha",
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert mgr.status_calls == ["alpha"]
        assert out["models"][0]["label"] == "alpha"

    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr", [_StubChatManager()], indirect=True,
    )
    def test_shutdown_all(self, running_daemon_with_chat_mgr):
        host, port, secret_path, mgr = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_shutdown(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out["stopped"] is True
        assert mgr.shutdown_calls == [None]

    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr", [_StubChatManager()], indirect=True,
    )
    def test_shutdown_one(self, running_daemon_with_chat_mgr):
        host, port, secret_path, mgr = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_shutdown(
            "beta",
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert mgr.shutdown_calls == ["beta"]
        assert out["label"] == "beta"

    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr",
        [_StubChatManager(gc_result={"evicted": ["orphan-1", "orphan-2"]})],
        indirect=True,
    )
    def test_gc_returns_evicted_list(self, running_daemon_with_chat_mgr):
        host, port, secret_path, mgr = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_gc(
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out["evicted"] == ["orphan-1", "orphan-2"]
        assert mgr.gc_calls == 1

    @pytest.mark.parametrize(
        "running_daemon_with_chat_mgr",
        [_StubChatManager(ensure_raises=RuntimeError("port 38093 in use"))],
        indirect=True,
    )
    def test_ensure_runtime_error_returned_as_unavailable(
        self, running_daemon_with_chat_mgr
    ):
        host, port, secret_path, _ = running_daemon_with_chat_mgr
        out = daemon_client.chat_model_ensure(
            "alpha",
            host=host, port=port, secret_path=secret_path, timeout=2.0,
        )
        assert out["available"] is False
        assert "port 38093 in use" in out["reason"]


# --------------------------------------------------------------------- #
# DaemonServer.request_shutdown reaps the chat manager too
# --------------------------------------------------------------------- #

class TestRequestShutdownReapsChatManager:
    def test_request_shutdown_reaps_chat_manager(self):
        secret = "x" * 64
        mgr = _StubChatManager()
        srv = daemon.DaemonServer(
            "127.0.0.1", 0, secret=secret,
            chat_model_manager=mgr,
        )
        try:
            srv.request_shutdown()
            # shutdown(None) → reap all
            assert mgr.shutdown_calls == [None]
        finally:
            srv.server_close()

    def test_request_shutdown_with_both_managers(self):
        """Both embedding and chat managers are reaped on graceful stop."""
        secret = "x" * 64

        class _EmbStub:
            def __init__(self):
                self.shutdown_calls = 0
            def shutdown(self):
                self.shutdown_calls += 1

        emb = _EmbStub()
        chat = _StubChatManager()
        srv = daemon.DaemonServer(
            "127.0.0.1", 0, secret=secret,
            embedding_manager=emb, chat_model_manager=chat,
        )
        try:
            srv.request_shutdown()
            assert emb.shutdown_calls == 1
            assert chat.shutdown_calls == [None]
        finally:
            srv.server_close()


# --------------------------------------------------------------------- #
# Client wrappers — pure mock (no real daemon)
# --------------------------------------------------------------------- #

class TestClientWrappersMocked:
    def test_chat_model_ensure_returns_none_when_daemon_down(self):
        with patch("claude_hooks.daemon_client.call", return_value=None):
            assert daemon_client.chat_model_ensure("alpha") is None

    def test_chat_model_ensure_returns_result_on_ok(self):
        with patch("claude_hooks.daemon_client.call",
                   return_value={"ok": True, "result": {"ready": True}}):
            out = daemon_client.chat_model_ensure("alpha")
            assert out == {"ready": True}

    def test_chat_model_ensure_returns_unavailable_on_nack(self):
        with patch("claude_hooks.daemon_client.call",
                   return_value={"ok": False, "error": "boom"}):
            out = daemon_client.chat_model_ensure("alpha")
            assert out == {"available": False, "reason": "boom"}

    def test_chat_model_status_no_label_sends_empty_payload(self):
        captured = {}

        def _spy(event, payload, **kw):
            captured["payload"] = payload
            return {"ok": True, "result": {"models": []}}

        with patch("claude_hooks.daemon_client.call", side_effect=_spy):
            daemon_client.chat_model_status()
        assert captured["payload"] == {}

    def test_chat_model_status_with_label_sends_label(self):
        captured = {}

        def _spy(event, payload, **kw):
            captured["payload"] = payload
            return {"ok": True, "result": {"models": []}}

        with patch("claude_hooks.daemon_client.call", side_effect=_spy):
            daemon_client.chat_model_status("alpha")
        assert captured["payload"] == {"label": "alpha"}

    def test_chat_model_shutdown_no_label_sends_empty(self):
        captured = {}

        def _spy(event, payload, **kw):
            captured["payload"] = payload
            return {"ok": True, "result": {"stopped": True}}

        with patch("claude_hooks.daemon_client.call", side_effect=_spy):
            daemon_client.chat_model_shutdown()
        assert captured["payload"] == {}

    def test_chat_model_shutdown_with_label(self):
        captured = {}

        def _spy(event, payload, **kw):
            captured["payload"] = payload
            return {"ok": True, "result": {"stopped": True, "label": "x"}}

        with patch("claude_hooks.daemon_client.call", side_effect=_spy):
            daemon_client.chat_model_shutdown("x")
        assert captured["payload"] == {"label": "x"}

    def test_chat_model_gc_returns_none_when_daemon_down(self):
        with patch("claude_hooks.daemon_client.call", return_value=None):
            assert daemon_client.chat_model_gc() is None

    def test_chat_model_gc_returns_result_on_ok(self):
        with patch("claude_hooks.daemon_client.call",
                   return_value={"ok": True,
                                 "result": {"evicted": ["foo"]}}):
            out = daemon_client.chat_model_gc()
            assert out == {"evicted": ["foo"]}
