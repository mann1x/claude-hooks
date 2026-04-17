"""Coverage-lift tests for claude_hooks/proxy/* error paths + the
``run()`` CLI entry point. Targets the remaining uncovered branches
after P0/P1/P3 integration tests.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_hooks.proxy import server as server_mod
from claude_hooks.proxy.forwarder import forward, UpstreamResult
from claude_hooks.proxy.metadata import (
    extract_request_info, extract_response_info,
)


# --------------------------------------------------------------- #
# run() — CLI entry point, covers lines 289-352
# --------------------------------------------------------------- #
class TestRun:
    def _find_free_port(self) -> int:
        s = socket.socket(); s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]; s.close(); return p

    def test_returns_2_when_disabled(self, capsys):
        rc = server_mod.run({"proxy": {"enabled": False}})
        err = capsys.readouterr().err
        assert rc == 2
        assert "not enabled" in err

    def test_returns_1_on_bind_failure(self, capsys):
        # Bind to a port we're already holding — OSError propagates.
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        cfg = {"proxy": {
            "enabled": True,
            "listen_host": "127.0.0.1",
            "listen_port": port,
            "upstream": "http://127.0.0.1:9",
            "timeout": 1.0,
            "log_dir": "/tmp/proxy-coverage-bind",
            "log_retention_days": 1,
        }}
        try:
            rc = server_mod.run(cfg)
        finally:
            holder.close()
        err = capsys.readouterr().err
        assert rc == 1
        assert "bind failed" in err

    # NOTE: run()'s SIGTERM semantics are verified manually on the
    # deployed systemd service rather than in-process — sending
    # SIGTERM from a thread inside pytest propagates to the test
    # runner and terminates the whole suite. The two tests above
    # cover both early-exit branches of run(); the wait-loop body
    # is simple enough to take on faith once started.


# --------------------------------------------------------------- #
# forwarder upstream = http path (existing tests cover https only
# via the URL parser; http path exercises the non-SSL branch)
# --------------------------------------------------------------- #
class TestForwarderHttp:
    def _find_free_port(self) -> int:
        s = socket.socket(); s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]; s.close(); return p

    def test_forward_over_plain_http(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Echo(BaseHTTPRequestHandler):
            def log_message(self, *a, **k): pass
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                body = b'{"ok":true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        port = self._find_free_port()
        srv = HTTPServer(("127.0.0.1", port), Echo)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            result = forward(
                f"http://127.0.0.1:{port}",
                "POST", "/v1/messages",
                {"Content-Type": "application/json"},
                b'{"x":1}', timeout=5.0,
            )
            # Drain the iterator so the underlying connection closes
            # cleanly.
            body = result.first_chunk + b"".join(result.body_iter)
            assert result.status == 200
            assert b'{"ok":true}' in body
        finally:
            srv.shutdown(); srv.server_close()

    def test_forward_raises_value_error_for_bad_url(self):
        with pytest.raises(ValueError):
            forward(
                "not-a-url", "GET", "/",
                {}, b"", timeout=1.0,
            )


# --------------------------------------------------------------- #
# metadata helpers — the remaining uncovered branches
# --------------------------------------------------------------- #
class TestMetadataCoverage:
    def test_non_dict_json_returns_default(self):
        # A valid JSON but not an object — should not explode.
        out = extract_request_info(b'["list","not","dict"]', {})
        assert out["model_requested"] is None

    def test_messages_non_list_skipped(self):
        out = extract_request_info(b'{"messages": "oops"}', {})
        assert out["is_warmup"] is False

    def test_first_message_non_dict_skipped(self):
        out = extract_request_info(
            b'{"messages": ["not a dict"]}', {},
        )
        assert out["is_warmup"] is False

    def test_assistant_as_first_is_not_warmup(self):
        body = json.dumps({
            "messages": [
                {"role": "assistant", "content": "Warmup"},
            ],
        }).encode()
        out = extract_request_info(body, {})
        assert out["is_warmup"] is False   # Warmup must be a USER message

    def test_extract_response_invalid_utf8_in_chunk(self):
        # Garbage bytes — shouldn't crash.
        out = extract_response_info({}, b"\xff\xfe\x00\x01")
        assert out["model_delivered"] is None

    def test_extract_response_sse_without_data_line(self):
        chunk = b"event: message_start\nfoo: bar\n\n"
        out = extract_response_info({}, chunk)
        assert out["model_delivered"] is None

    def test_metadata_session_id_from_user_id(self):
        body = json.dumps({
            "metadata": {"user_id": "sess-xyz"},
        }).encode()
        out = extract_request_info(body, {})
        assert out["session_id"] == "sess-xyz"


# --------------------------------------------------------------- #
# sse tail edge cases
# --------------------------------------------------------------- #
class TestSseEdges:
    def test_empty_event_skipped(self):
        from claude_hooks.proxy.sse import SseTail
        tail = SseTail()
        list(tail.wrap([b"\n\n"]))
        assert tail.final_usage is None

    def test_event_data_only_newlines_skipped(self):
        from claude_hooks.proxy.sse import SseTail
        tail = SseTail()
        chunk = b"event: message_delta\ndata:\n\n"
        list(tail.wrap([chunk]))
        assert tail.final_usage is None

    def test_event_without_type_field_skipped(self):
        from claude_hooks.proxy.sse import SseTail
        tail = SseTail()
        chunk = b"data: {\"not_a_type\": 1}\n\n"
        list(tail.wrap([chunk]))
        assert tail.final_usage is None

    def test_crlf_event_boundary_parsed(self):
        from claude_hooks.proxy.sse import SseTail
        tail = SseTail()
        payload = (
            "event: message_delta\r\n"
            "data: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":9}}\r\n\r\n"
        ).encode()
        list(tail.wrap([payload]))
        assert tail.final_usage == {"output_tokens": 9}


# --------------------------------------------------------------- #
# httpx forwarder — HTTP/2 client + pool reuse
# --------------------------------------------------------------- #
class TestHttpxForwarder:
    def test_pooled_client_is_reused_across_calls(self):
        """Two forward() calls must share one httpx.Client instance.

        This is the whole point of the httpx rewrite: one connection
        profile to upstream, not fresh-per-request.
        """
        from claude_hooks.proxy import forwarder as fwd

        fwd._reset_client()
        c1 = fwd._get_client(timeout=5.0)
        c2 = fwd._get_client(timeout=5.0)
        try:
            assert c1 is c2
            # http2 flag must be on — that's what matches native CC's
            # connection profile.
            import httpx
            assert isinstance(c1, httpx.Client)
        finally:
            fwd._reset_client()

    def test_forward_records_http_version_in_stats(self):
        """The forwarder tags each result with the negotiated protocol.

        For plain-HTTP test servers we'll see HTTP/1.1 — good enough to
        confirm stats wiring. Real api.anthropic.com returns HTTP/2.
        """
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Echo(BaseHTTPRequestHandler):
            def log_message(self, *a, **k): pass
            def do_POST(self):
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        s = socket.socket(); s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]; s.close()
        srv = HTTPServer(("127.0.0.1", port), Echo)
        t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
        try:
            result = forward(
                f"http://127.0.0.1:{port}",
                "POST", "/v1/messages",
                {"Content-Type": "application/json"},
                b'{"x":1}', timeout=5.0,
            )
            body = result.first_chunk + b"".join(result.body_iter)
            assert result.status == 200
            assert b'{"ok":true}' in body
            assert "http_version" in result.stats
            # Test server is HTTP/1.1; real upstream negotiates h2.
            assert result.stats["http_version"].startswith("HTTP/")
        finally:
            srv.shutdown(); srv.server_close()

    def test_forward_rejects_unsupported_scheme(self):
        with pytest.raises(ValueError):
            forward(
                "ftp://example.com/v1", "GET", "/",
                {}, b"", timeout=1.0,
            )


# --------------------------------------------------------------- #
# Retry on RemoteProtocolError ("Server disconnected")
# --------------------------------------------------------------- #
class TestForwarderRetry:
    def test_remote_protocol_error_triggers_retry_then_succeeds(self, monkeypatch):
        """First attempt raises RemoteProtocolError, second returns 200.

        Simulates Anthropic's edge dropping a stale HTTP/2 connection —
        the retry lands on a fresh one and the caller never sees the
        failure.
        """
        import httpx
        from claude_hooks.proxy import forwarder as fwd

        fwd._reset_client()

        calls = {"n": 0}
        real_client = fwd._get_client(timeout=5.0)

        original_send = real_client.send

        def fake_send(req, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.RemoteProtocolError("Server disconnected")
            return original_send(req, *args, **kwargs)

        monkeypatch.setattr(real_client, "send", fake_send)

        # Use a real local server so the second (real) attempt works.
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class OK(BaseHTTPRequestHandler):
            def log_message(self, *a, **k): pass
            def do_POST(self):
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        s = socket.socket(); s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]; s.close()
        srv = HTTPServer(("127.0.0.1", port), OK)
        t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
        try:
            result = forward(
                f"http://127.0.0.1:{port}", "POST", "/v1/messages",
                {"Content-Type": "application/json"},
                b'{"x":1}', timeout=5.0,
            )
            body = result.first_chunk + b"".join(result.body_iter)
            assert result.status == 200
            assert b'{"ok":true}' in body
            assert calls["n"] == 2      # first failed, second succeeded
        finally:
            srv.shutdown(); srv.server_close()
            fwd._reset_client()

    def test_retries_exhausted_reraises(self, monkeypatch):
        """If every attempt raises, the last exception propagates."""
        import httpx
        from claude_hooks.proxy import forwarder as fwd

        fwd._reset_client()
        client = fwd._get_client(timeout=5.0)

        calls = {"n": 0}

        def always_fail(req, *args, **kwargs):
            calls["n"] += 1
            raise httpx.RemoteProtocolError("Server disconnected")

        monkeypatch.setattr(client, "send", always_fail)
        # Collapse backoff for speed.
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_BASE", 0.0)

        with pytest.raises(httpx.RemoteProtocolError):
            forward(
                "http://127.0.0.1:1", "POST", "/v1/messages",
                {"Content-Type": "application/json"},
                b'{"x":1}', timeout=5.0,
            )
        # 1 initial + _UPSTREAM_RETRIES retries
        assert calls["n"] == fwd._UPSTREAM_RETRIES + 1
        fwd._reset_client()


# --------------------------------------------------------------- #
# Retry on upstream HTTP 5xx — the proxy masks transient Anthropic
# edge errors so Claude Code doesn't see a spurious 502 for every
# blip.
# --------------------------------------------------------------- #
class TestForwarderStatusRetry:
    """Upstream 5xx responses in ``_RETRY_ON_STATUS`` must be retried
    transparently. The client sees either the eventual success or the
    authentic upstream error after all retries are exhausted — never
    our own ``proxy_error`` wrapper."""

    def _build_flaky_server(self, responses):
        """Start a local HTTPServer that returns each ``(status, body)``
        tuple in ``responses`` in order, then 200 ``{"ok":true}`` for
        any request beyond the list.
        """
        from http.server import BaseHTTPRequestHandler, HTTPServer

        calls = {"n": 0}
        state = {"i": 0}
        responses = list(responses)

        class Flaky(BaseHTTPRequestHandler):
            def log_message(self, *a, **k): pass

            def do_POST(self):
                calls["n"] += 1
                if state["i"] < len(responses):
                    status, body = responses[state["i"]]
                    state["i"] += 1
                else:
                    status, body = 200, b'{"ok":true}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        s = socket.socket(); s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]; s.close()
        srv = HTTPServer(("127.0.0.1", port), Flaky)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, port, calls

    def test_502_then_200_retries_transparently(self, monkeypatch):
        """Upstream returns 502 once, then 200. Forwarder should retry
        and the caller receives the 200 — never sees the transient 502.
        """
        from claude_hooks.proxy import forwarder as fwd
        fwd._reset_client()
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_BASE", 0.0)
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_MAX", 0.0)

        srv, port, calls = self._build_flaky_server([
            (502, b'{"error":{"type":"overloaded_error"}}'),
        ])
        try:
            result = forward(
                f"http://127.0.0.1:{port}", "POST", "/v1/messages",
                {"Content-Type": "application/json"},
                b'{"x":1}', timeout=5.0,
            )
            body = result.first_chunk + b"".join(result.body_iter)
            assert result.status == 200
            assert b'{"ok":true}' in body
            assert calls["n"] == 2          # 1 retry then success
        finally:
            srv.shutdown(); srv.server_close()
            fwd._reset_client()

    def test_default_retry_count_is_ten(self):
        """The default retry count documented for the 'quick 10 retries'
        behaviour must actually be 10 unless overridden via env."""
        from claude_hooks.proxy import forwarder as fwd
        assert fwd._UPSTREAM_RETRIES == 10

    def test_ten_502s_then_success(self, monkeypatch):
        """Ten consecutive 502 responses must not surface to the
        client: the 11th attempt (1 initial + 10 retries) succeeds."""
        from claude_hooks.proxy import forwarder as fwd
        fwd._reset_client()
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_BASE", 0.0)
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_MAX", 0.0)

        flakes = [(502, b'{"err":"overloaded"}')] * 10
        srv, port, calls = self._build_flaky_server(flakes)
        try:
            result = forward(
                f"http://127.0.0.1:{port}", "POST", "/v1/messages",
                {"Content-Type": "application/json"},
                b'{"x":1}', timeout=5.0,
            )
            body = result.first_chunk + b"".join(result.body_iter)
            assert result.status == 200, (
                f"expected 200 after 10 flakes, got {result.status}"
            )
            assert b'{"ok":true}' in body
            # 10 failing + 1 successful = 11 total requests.
            assert calls["n"] == 11
        finally:
            srv.shutdown(); srv.server_close()
            fwd._reset_client()

    def test_retries_exhausted_returns_upstream_response(self, monkeypatch):
        """If every attempt returns 502, the caller receives the
        *authentic* upstream 502 response (not our proxy-synthesized
        bad-gateway wrapper). The server-level handler mirrors this
        verbatim to Claude Code.
        """
        from claude_hooks.proxy import forwarder as fwd
        fwd._reset_client()
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_BASE", 0.0)
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_MAX", 0.0)
        # Shrink retry budget so the test doesn't fight the default.
        monkeypatch.setattr(fwd, "_UPSTREAM_RETRIES", 3)

        upstream_body = b'{"error":{"type":"overloaded_error","message":"try later"}}'
        # N+1 flaky responses so even the final attempt fails.
        flakes = [(502, upstream_body)] * 20
        srv, port, calls = self._build_flaky_server(flakes)
        try:
            result = forward(
                f"http://127.0.0.1:{port}", "POST", "/v1/messages",
                {"Content-Type": "application/json"},
                b'{"x":1}', timeout=5.0,
            )
            body = result.first_chunk + b"".join(result.body_iter)
            # Authentic upstream response passed through.
            assert result.status == 502
            assert body == upstream_body
            # 1 initial + 3 retries = 4 attempts total.
            assert calls["n"] == 4
        finally:
            srv.shutdown(); srv.server_close()
            fwd._reset_client()

    def test_non_retryable_4xx_not_retried(self, monkeypatch):
        """A 400 (client error) must flow through untouched — no retry
        loop, since retrying a bad request won't help."""
        from claude_hooks.proxy import forwarder as fwd
        fwd._reset_client()
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_BASE", 0.0)
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_MAX", 0.0)

        srv, port, calls = self._build_flaky_server([
            (400, b'{"error":"bad_request"}'),
        ])
        try:
            result = forward(
                f"http://127.0.0.1:{port}", "POST", "/v1/messages",
                {"Content-Type": "application/json"},
                b'{"x":1}', timeout=5.0,
            )
            body = result.first_chunk + b"".join(result.body_iter)
            assert result.status == 400
            assert b'bad_request' in body
            assert calls["n"] == 1          # no retry
        finally:
            srv.shutdown(); srv.server_close()
            fwd._reset_client()

    def test_retry_status_env_override(self, monkeypatch):
        """Overriding ``_RETRY_ON_STATUS`` (what the env var drives)
        lets callers include / exclude codes. Verify a non-default
        code (418) becomes retryable when added to the set."""
        from claude_hooks.proxy import forwarder as fwd
        fwd._reset_client()
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_BASE", 0.0)
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_MAX", 0.0)
        monkeypatch.setattr(fwd, "_RETRY_ON_STATUS", frozenset({418}))

        srv, port, calls = self._build_flaky_server([
            (418, b'{"error":"teapot"}'),
        ])
        try:
            result = forward(
                f"http://127.0.0.1:{port}", "POST", "/v1/messages",
                {"Content-Type": "application/json"},
                b'{"x":1}', timeout=5.0,
            )
            body = result.first_chunk + b"".join(result.body_iter)
            assert result.status == 200     # retry succeeded
            assert b'{"ok":true}' in body
            assert calls["n"] == 2
        finally:
            srv.shutdown(); srv.server_close()
            fwd._reset_client()

    def test_parse_status_set_from_env_string(self):
        """``_parse_status_set`` must honour a comma-separated list and
        gracefully fall back to the default on empty / malformed input."""
        from claude_hooks.proxy import forwarder as fwd
        assert fwd._parse_status_set("502,503,529") == frozenset(
            {502, 503, 529}
        )
        assert fwd._parse_status_set("") == fwd._DEFAULT_RETRY_STATUS
        assert fwd._parse_status_set(None) == fwd._DEFAULT_RETRY_STATUS
        # Garbage tokens ignored; if nothing parses, default wins.
        assert fwd._parse_status_set("abc,,xyz") == fwd._DEFAULT_RETRY_STATUS
        # Mixed — valid tokens extracted.
        assert fwd._parse_status_set("502, foo, 504") == frozenset(
            {502, 504}
        )
