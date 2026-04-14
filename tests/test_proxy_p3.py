"""
Phase-P3 tests: block_warmup short-circuits without hitting upstream.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from claude_hooks.proxy.server import build_server
from claude_hooks.proxy.stub import build_non_streaming, build_streaming


# ================================================================ #
# Unit: stub builders
# ================================================================ #
class TestStubBuilders:
    def test_non_streaming_body_is_valid_anthropic_message(self):
        status, headers, body = build_non_streaming("claude-haiku-4-5", "msg_x")
        assert status == 200
        assert headers["Content-Type"] == "application/json"
        payload = json.loads(body)
        assert payload["type"] == "message"
        assert payload["role"] == "assistant"
        assert payload["model"] == "claude-haiku-4-5"
        assert payload["stop_reason"] == "end_turn"
        assert payload["usage"]["input_tokens"] == 0
        assert payload["usage"]["output_tokens"] == 0

    def test_streaming_body_has_all_sse_events(self):
        status, headers, body = build_streaming("claude-opus-4-6", "msg_y")
        assert status == 200
        assert headers["Content-Type"] == "text/event-stream"
        text = body.decode()
        # Events in order, each terminated by \n\n.
        for evt in [
            "event: message_start",
            "event: content_block_start",
            "event: content_block_delta",
            "event: content_block_stop",
            "event: message_delta",
            "event: message_stop",
        ]:
            assert evt in text
        # Model echoed back in message_start.
        assert "claude-opus-4-6" in text

    def test_stub_emits_marker_header(self):
        _, headers, _ = build_non_streaming("m", "id")
        assert headers.get("X-Claude-Hooks-Proxy") == "warmup-blocked"


# ================================================================ #
# Integration: proxy short-circuits Warmup
# ================================================================ #
def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _CountingUpstream(BaseHTTPRequestHandler):
    call_count: int = 0

    def log_message(self, *a, **kw): pass

    def do_POST(self):
        _CountingUpstream.call_count += 1
        body = b'{"model":"x","stop_reason":"end_turn","usage":{"input_tokens":0}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST


@pytest.fixture
def counting_upstream():
    _CountingUpstream.call_count = 0
    port = _find_free_port()
    srv = HTTPServer(("127.0.0.1", port), _CountingUpstream)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", _CountingUpstream
    finally:
        srv.shutdown(); srv.server_close()


def _start_proxy(cfg, tmp_path):
    server, _ = build_server(cfg)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def _cfg(upstream_url: str, tmp_path: Path, *, block: bool) -> dict:
    return {"proxy": {
        "enabled": True,
        "listen_host": "127.0.0.1",
        "listen_port": _find_free_port(),
        "upstream": upstream_url,
        "timeout": 5.0,
        "log_requests": True,
        "log_dir": str(tmp_path / "log"),
        "log_retention_days": 1,
        "record_rate_limit_headers": True,
        "block_warmup": block,
    }}


def _post(base: str, body: dict, *, stream: bool = False) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(
        base + "/v1/messages",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    return resp.status, resp.read(), dict(resp.headers.items())


class TestBlockWarmup:
    def test_warmup_blocked_does_not_call_upstream(self, counting_upstream, tmp_path):
        upstream_url, upstream_h = counting_upstream
        cfg = _cfg(upstream_url, tmp_path, block=True)
        server, base = _start_proxy(cfg, tmp_path)
        try:
            status, body, headers = _post(base, {
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "Warmup"}],
            })
        finally:
            server.shutdown(); server.server_close()
        assert status == 200
        assert headers.get("X-Claude-Hooks-Proxy") == "warmup-blocked"
        payload = json.loads(body)
        assert payload["stop_reason"] == "end_turn"
        assert upstream_h.call_count == 0     # UPSTREAM NEVER CALLED
        time.sleep(0.05)
        # Log records warmup_blocked=True.
        lines = list((tmp_path / "log").glob("*.jsonl"))
        rec = json.loads(lines[0].read_text().strip())
        assert rec["warmup_blocked"] is True
        assert rec["is_warmup"] is True

    def test_warmup_streaming_returns_sse_stub(self, counting_upstream, tmp_path):
        upstream_url, upstream_h = counting_upstream
        cfg = _cfg(upstream_url, tmp_path, block=True)
        server, base = _start_proxy(cfg, tmp_path)
        try:
            status, body, headers = _post(base, {
                "model": "claude-opus-4-6",
                "stream": True,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Warmup"}
                ]}],
            })
        finally:
            server.shutdown(); server.server_close()
        assert status == 200
        assert "text/event-stream" in headers.get("Content-Type", "")
        assert b"event: message_start" in body
        assert b"event: message_stop" in body
        assert upstream_h.call_count == 0

    def test_non_warmup_still_forwarded(self, counting_upstream, tmp_path):
        upstream_url, upstream_h = counting_upstream
        cfg = _cfg(upstream_url, tmp_path, block=True)
        server, base = _start_proxy(cfg, tmp_path)
        try:
            status, body, _ = _post(base, {
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "real work"}],
            })
        finally:
            server.shutdown(); server.server_close()
        assert status == 200
        # Upstream was called — content came from our CountingUpstream.
        assert upstream_h.call_count == 1

    def test_block_warmup_false_forwards_warmup(self, counting_upstream, tmp_path):
        upstream_url, upstream_h = counting_upstream
        cfg = _cfg(upstream_url, tmp_path, block=False)
        server, base = _start_proxy(cfg, tmp_path)
        try:
            _post(base, {
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "Warmup"}],
            })
        finally:
            server.shutdown(); server.server_close()
        # With block_warmup=false we pass Warmups through.
        assert upstream_h.call_count == 1
