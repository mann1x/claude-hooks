"""Tests for the cross-host embedder admission gate (Layer 2).

Layer 1 (``store_lock``) serialises stores within a host. This layer
handles the case Layer 1 structurally cannot: two *machines* sharing one
embedder. It keys on the embedder rather than the memory backend so it
works for every provider — a Postgres advisory lock would have covered
only pgvector.

The busy oracle is unusual and worth restating, because a future reader
will otherwise "fix" it: llama.cpp answers ``/health`` and ``/props``
straight from the HTTP threads (instant even at full CPU load) but
serves ``/slots`` by queueing a task into the single-consumer inference
loop. So a ``/slots`` timeout *is* the occupancy signal, not a failure.
Measured on solidpc during a 29 s embed: ``/health`` 0.00 s throughout,
``/slots`` 5.6-11.7 s.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import embedder_gate as eg  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    # set per-server
    mode = "idle"
    delay = 0.0

    def do_GET(self):  # noqa: N802
        if self.path != "/slots":
            self.send_response(404)
            self.end_headers()
            return
        if self.server.mode == "hang":            # type: ignore[attr-defined]
            time.sleep(self.server.delay)         # type: ignore[attr-defined]
        if self.server.mode == "notfound":        # type: ignore[attr-defined]
            self.send_response(501)
            self.end_headers()
            return
        busy = self.server.mode == "busy"         # type: ignore[attr-defined]
        body = json.dumps(
            [{"id": i, "is_processing": bool(busy and i == 0)} for i in range(4)]
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.mode = "idle"      # type: ignore[attr-defined]
    srv.delay = 0.0        # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address[0], srv.server_address[1]
    yield srv, f"http://{host}:{port}/embedding"
    srv.shutdown()
    srv.server_close()


class TestSlotsUrl:
    def test_derives_slots_from_embedding_url(self):
        assert (eg.slots_url("http://h:38092/embedding")
                == "http://h:38092/slots")

    def test_rejects_non_http(self):
        assert eg.slots_url("") is None
        assert eg.slots_url("llamafile://local") is None
        assert eg.slots_url("/just/a/path") is None


class TestProbe:
    def test_idle(self, server):
        srv, url = server
        srv.mode = "idle"          # type: ignore[attr-defined]
        assert eg.probe(url, timeout=5) == eg.IDLE

    def test_busy_when_a_slot_is_processing(self, server):
        srv, url = server
        srv.mode = "busy"          # type: ignore[attr-defined]
        assert eg.probe(url, timeout=5) == eg.BUSY

    def test_timeout_reads_as_busy(self, server):
        """The load-bearing inversion: a stalled /slots means the
        inference loop is busy, so a timeout is BUSY, not an error."""
        srv, url = server
        srv.mode = "hang"          # type: ignore[attr-defined]
        srv.delay = 3.0            # type: ignore[attr-defined]
        t0 = time.monotonic()
        assert eg.probe(url, timeout=0.4) == eg.BUSY
        assert time.monotonic() - t0 < 2.5, "probe must not wait out the hang"

    def test_non_llamacpp_endpoint_is_unknown(self, server):
        """Ollama / OpenAI-compatible endpoints have no /slots — the
        gate must disable itself rather than block every store."""
        srv, url = server
        srv.mode = "notfound"      # type: ignore[attr-defined]
        assert eg.probe(url, timeout=5) == eg.UNKNOWN

    def test_unroutable_url_is_unknown(self):
        assert eg.probe("not-a-url", timeout=1) == eg.UNKNOWN


class TestWaitForCapacity:
    def test_returns_immediately_when_idle(self, server):
        srv, url = server
        srv.mode = "idle"          # type: ignore[attr-defined]
        t0 = time.monotonic()
        assert eg.wait_for_capacity(url, max_wait=5, probe_timeout=2,
                                    poll_interval=0.1) == eg.IDLE
        assert time.monotonic() - t0 < 2.0

    def test_gives_up_and_lets_the_store_proceed(self, server):
        """Bounded: a delayed store is fine, a dropped one is not."""
        srv, url = server
        srv.mode = "busy"          # type: ignore[attr-defined]
        t0 = time.monotonic()
        assert eg.wait_for_capacity(url, max_wait=0.6, probe_timeout=2,
                                    poll_interval=0.2) == eg.BUSY
        assert time.monotonic() - t0 < 5.0

    def test_unknown_endpoint_does_not_wait(self, server):
        srv, url = server
        srv.mode = "notfound"      # type: ignore[attr-defined]
        t0 = time.monotonic()
        assert eg.wait_for_capacity(url, max_wait=10, probe_timeout=2,
                                    poll_interval=1) == eg.UNKNOWN
        assert time.monotonic() - t0 < 3.0

    def test_proceeds_once_the_embedder_frees_up(self, server):
        srv, url = server
        srv.mode = "busy"          # type: ignore[attr-defined]

        def free_it():
            time.sleep(0.5)
            srv.mode = "idle"      # type: ignore[attr-defined]

        threading.Thread(target=free_it, daemon=True).start()
        assert eg.wait_for_capacity(url, max_wait=10, probe_timeout=2,
                                    poll_interval=0.2) == eg.IDLE

    def test_zero_budget_is_a_noop(self, server):
        _, url = server
        assert eg.wait_for_capacity(url, max_wait=0) == eg.UNKNOWN


class TestConfig:
    def test_disabled_by_default(self):
        c = eg.config_from({})
        assert c["enabled"] is False
        assert c["max_wait_s"] == eg.DEFAULT_MAX_WAIT_S
        assert c["probe_timeout_s"] == eg.DEFAULT_PROBE_TIMEOUT_S

    def test_reads_block(self):
        c = eg.config_from({"hooks": {"stop": {"embedder_gate": {
            "enabled": True, "max_wait_s": 5, "probe_timeout_s": 0.5,
            "poll_interval_s": 1}}}})
        assert c["enabled"] is True
        assert c["max_wait_s"] == 5.0
        assert c["probe_timeout_s"] == 0.5

    def test_embed_url_from_provider(self):
        cfg = {"providers": {"pgvector": {"embedder_options": {
            "url": "http://192.168.178.2:38092/embedding"}}}}
        assert eg.embed_url_from(cfg, ["pgvector"]) == \
            "http://192.168.178.2:38092/embedding"

    def test_embed_url_none_when_absent(self):
        assert eg.embed_url_from({"providers": {"pgvector": {}}},
                                 ["pgvector"]) is None

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOOKS_EMBED_URL", "http://h:1/embedding")
        assert eg.embed_url_from({}, []) == "http://h:1/embedding"


class TestStoreAsyncIntegration:
    def test_gate_is_skipped_when_disabled(self, monkeypatch):
        from claude_hooks import store_async
        called = []
        monkeypatch.setattr(eg, "wait_for_capacity",
                            lambda *a, **k: called.append(1))
        store_async._wait_for_embedder({}, ["pgvector"])
        assert called == []

    def test_gate_runs_when_enabled(self, monkeypatch):
        from claude_hooks import store_async
        called = []
        monkeypatch.setattr(eg, "wait_for_capacity",
                            lambda *a, **k: called.append(a[0]) or eg.IDLE)
        cfg = {
            "hooks": {"stop": {"embedder_gate": {"enabled": True}}},
            "providers": {"pgvector": {"embedder_options": {
                "url": "http://h:38092/embedding"}}},
        }
        store_async._wait_for_embedder(cfg, ["pgvector"])
        assert called == ["http://h:38092/embedding"]

    def test_gate_failure_never_blocks_the_store(self, monkeypatch):
        from claude_hooks import store_async

        def boom(*a, **k):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(eg, "wait_for_capacity", boom)
        cfg = {
            "hooks": {"stop": {"embedder_gate": {"enabled": True}}},
            "providers": {"pgvector": {"embedder_options": {
                "url": "http://h:38092/embedding"}}},
        }
        store_async._wait_for_embedder(cfg, ["pgvector"])  # must not raise
