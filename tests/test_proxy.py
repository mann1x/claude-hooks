"""
Phase-P0 tests for the proxy — pass-through, logging, metadata extraction.

Strategy: start a tiny in-process fake upstream HTTP server on a
loopback port, point the proxy at it via config, send a client
request through the proxy, then assert on what the upstream received
and what landed in the JSONL log.

Keep tests < 1 s each by using short timeouts and tight body sizes.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import pytest

from claude_hooks.proxy.server import build_server
from claude_hooks.proxy.metadata import (
    extract_request_info, extract_response_info,
)


# -------------------------------------------------------------- #
# Fake upstream — echoes request headers + body, returns canned JSON
# -------------------------------------------------------------- #
class _UpstreamHandler(BaseHTTPRequestHandler):
    captured: list = []                       # filled by each request
    response_payload: dict = {}               # the JSON body to return
    response_status: int = 200
    response_headers: dict = {}

    def log_message(self, *a, **kw): pass     # silence stderr

    def _handle(self):
        clen = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(clen) if clen else b""
        self.captured.append({
            "method": self.command,
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": body,
        })
        payload = json.dumps(self.response_payload).encode("utf-8")
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in self.response_headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _handle


def _find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def upstream():
    _UpstreamHandler.captured = []
    _UpstreamHandler.response_payload = {}
    _UpstreamHandler.response_status = 200
    _UpstreamHandler.response_headers = {}
    port = _find_free_port()
    server = HTTPServer(("127.0.0.1", port), _UpstreamHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", _UpstreamHandler
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def proxy(upstream, tmp_path):
    """Start the proxy pointing at the fake upstream."""
    upstream_url, _ = upstream
    cfg = {
        "proxy": {
            "enabled": True,
            "listen_host": "127.0.0.1",
            "listen_port": _find_free_port(),
            "upstream": upstream_url,
            "timeout": 5.0,
            "log_requests": True,
            "log_dir": str(tmp_path / "proxy-log"),
            "log_retention_days": 1,
            "record_rate_limit_headers": True,
            "block_warmup": False,
        }
    }
    server, jsonl = build_server(cfg)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", Path(cfg["proxy"]["log_dir"]), jsonl
    finally:
        server.shutdown()
        server.server_close()


# -------------------------------------------------------------- #
# Core pass-through
# -------------------------------------------------------------- #
def _read_log(log_dir: Path) -> list[dict]:
    lines: list[dict] = []
    for f in log_dir.glob("*.jsonl"):
        for raw in f.read_text().splitlines():
            if raw.strip():
                lines.append(json.loads(raw))
    return lines


class TestProxyPassthrough:
    def test_get_is_forwarded(self, proxy, upstream):
        base, log_dir, _ = proxy
        upstream_url, h = upstream
        h.response_payload = {"ok": True}
        resp = urllib.request.urlopen(base + "/v1/ping", timeout=3)
        assert resp.status == 200
        assert json.loads(resp.read()) == {"ok": True}
        assert len(h.captured) == 1
        assert h.captured[0]["method"] == "GET"
        assert h.captured[0]["path"] == "/v1/ping"

    def test_post_body_is_forwarded(self, proxy, upstream):
        base, log_dir, _ = proxy
        _, h = upstream
        h.response_payload = {"model": "claude-opus-4-6",
                              "usage": {"input_tokens": 5, "output_tokens": 10}}
        body = json.dumps({
            "model": "claude-opus-4-6",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8")
        req = urllib.request.Request(
            base + "/v1/messages", data=body,
            headers={"Content-Type": "application/json",
                     "x-api-key": "sk-secret",
                     "anthropic-version": "2023-06-01"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=3)
        assert resp.status == 200
        # Upstream received the body.
        cap = h.captured[0]
        assert cap["method"] == "POST"
        assert cap["body"] == body
        # Auth header was preserved (case-insensitive — http.client may
        # Title-Case it).
        lc = {k.lower(): v for k, v in cap["headers"].items()}
        assert lc.get("x-api-key") == "sk-secret"

    def test_upstream_failure_yields_502(self, tmp_path, monkeypatch):
        # Point proxy at a TCP port nobody is listening on.
        # Shrink the forwarder retry budget so the 11-attempt default
        # doesn't exceed the client-side urllib timeout.
        from claude_hooks.proxy import forwarder as fwd
        monkeypatch.setattr(fwd, "_UPSTREAM_RETRIES", 1)
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_BASE", 0.0)
        monkeypatch.setattr(fwd, "_RETRY_BACKOFF_MAX", 0.0)
        fwd._reset_client()
        dead_port = _find_free_port()
        cfg = {
            "proxy": {
                "enabled": True,
                "listen_host": "127.0.0.1",
                "listen_port": _find_free_port(),
                "upstream": f"http://127.0.0.1:{dead_port}",
                "timeout": 2.0,
                "log_requests": True,
                "log_dir": str(tmp_path / "proxy-log"),
                "log_retention_days": 1,
                "record_rate_limit_headers": True,
                "block_warmup": False,
            }
        }
        server, _ = build_server(cfg)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        host, port = server.server_address
        try:
            # urllib raises on >=400 — catch via HTTPError.
            import urllib.error
            try:
                urllib.request.urlopen(
                    f"http://{host}:{port}/v1/ping", timeout=15,
                )
                assert False, "expected 502"
            except urllib.error.HTTPError as e:
                assert e.code == 502
        finally:
            server.shutdown()
            server.server_close()
            fwd._reset_client()


class TestProxyLogging:
    def test_request_logged_to_jsonl(self, proxy, upstream):
        base, log_dir, _ = proxy
        _, h = upstream
        h.response_payload = {"model": "claude-opus-4-6",
                              "usage": {"input_tokens": 12, "output_tokens": 34}}
        h.response_headers = {
            "anthropic-ratelimit-unified-5h-utilization": "0.42",
        }
        body = json.dumps({
            "model": "claude-opus-4-6",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8")
        req = urllib.request.Request(
            base + "/v1/messages", data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3).read()
        # Allow logger thread to flush.
        time.sleep(0.05)
        entries = _read_log(log_dir)
        assert len(entries) == 1
        e = entries[0]
        assert e["method"] == "POST"
        assert e["path"] == "/v1/messages"
        assert e["status"] == 200
        assert e["model_requested"] == "claude-opus-4-6"
        assert e["model_delivered"] == "claude-opus-4-6"
        assert e["usage"] == {"input_tokens": 12, "output_tokens": 34}
        assert e["rate_limit"] is not None
        assert "anthropic-ratelimit-unified-5h-utilization" in e["rate_limit"]
        assert e["is_warmup"] is False
        assert e["synthetic"] is False

    def test_warmup_flag_detected(self, proxy, upstream):
        base, log_dir, _ = proxy
        _, h = upstream
        h.response_payload = {"model": "claude-haiku-4-5"}
        body = json.dumps({
            "model": "claude-haiku-4-5",
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": "Warmup"}]}],
        }).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request(base + "/v1/messages", data=body,
                                   headers={"Content-Type": "application/json"},
                                   method="POST"),
            timeout=3,
        ).read()
        time.sleep(0.05)
        entries = _read_log(log_dir)
        assert len(entries) == 1
        assert entries[0]["is_warmup"] is True

    def test_synthetic_rate_limit_detected(self, proxy, upstream):
        base, log_dir, _ = proxy
        _, h = upstream
        h.response_payload = {
            "model": "<synthetic>",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        body = json.dumps({
            "model": "claude-opus-4-6",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request(base + "/v1/messages", data=body,
                                   headers={"Content-Type": "application/json"},
                                   method="POST"),
            timeout=3,
        ).read()
        time.sleep(0.05)
        entries = _read_log(log_dir)
        assert entries[0]["synthetic"] is True
        assert entries[0]["model_delivered"] == "<synthetic>"


# -------------------------------------------------------------- #
# Unit tests on the metadata extractors — no network
# -------------------------------------------------------------- #
class TestMetadataExtractors:
    def test_extract_request_warmup_string_content(self):
        body = json.dumps({
            "model": "x",
            "messages": [{"role": "user", "content": "Warmup"}],
        }).encode("utf-8")
        out = extract_request_info(body, {})
        assert out["is_warmup"] is True
        assert out["model_requested"] == "x"

    def test_extract_request_warmup_list_content(self):
        body = json.dumps({
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": "Warmup"}]}],
        }).encode("utf-8")
        out = extract_request_info(body, {})
        assert out["is_warmup"] is True

    def test_extract_request_not_warmup(self):
        body = json.dumps({
            "messages": [{"role": "user", "content": "hello there"}],
        }).encode("utf-8")
        out = extract_request_info(body, {})
        assert out["is_warmup"] is False

    def test_claude_p_sdk_cli_persona_classified_as_main(self):
        # `claude -p` in CC 2.1.119+ ships an SDK-style persona under
        # cc_entrypoint=sdk-cli with a single user message. Without the
        # second main-prefix entry this would fall into the subagent
        # branch and then be flipped to warmup by the SDK-priming
        # heuristic — silently dropping the user's prompt.
        body = json.dumps({
            "system": [
                {"type": "text", "text":
                 "x-anthropic-billing-header: cc_version=2.1.119; "
                 "cc_entrypoint=sdk-cli; cch=abc;"},
                {"type": "text", "text":
                 "You are a Claude agent, built on Anthropic's "
                 "Claude Agent SDK."},
                {"type": "text", "text": "...full instructions..."},
            ],
            "messages": [{"role": "user", "content": "Reply PONG"}],
        }).encode("utf-8")
        out = extract_request_info(body, {})
        assert out["agent_type"] == "main"
        assert out["is_warmup"] is False
        assert out["cc_entrypoint"] == "sdk-cli"

    def test_real_sdk_agent_priming_still_flagged_as_warmup(self):
        # A genuine SDK Agent() priming request: subagent persona,
        # sdk-cli entrypoint, one priming message. Should still be
        # caught by the heuristic so we don't lose the warmup-block
        # savings.
        body = json.dumps({
            "system": [
                {"type": "text", "text":
                 "x-anthropic-billing-header: cc_version=2.1.119; "
                 "cc_entrypoint=sdk-cli;"},
                {"type": "text", "text":
                 "You are a code reviewer specialized in TypeScript."},
            ],
            "messages": [{"role": "user", "content": "priming"}],
        }).encode("utf-8")
        out = extract_request_info(body, {})
        assert out["is_warmup"] is True
        assert out["agent_type"] == "warmup"

    def test_extract_request_invalid_json(self):
        out = extract_request_info(b"not json", {})
        assert out["model_requested"] is None
        assert out["is_warmup"] is False

    def test_extract_request_empty_body(self):
        out = extract_request_info(b"", {})
        assert out["model_requested"] is None

    def test_extract_response_json_shape(self):
        chunk = json.dumps({
            "model": "claude-opus-4-6",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }).encode("utf-8")
        out = extract_response_info(
            {"anthropic-ratelimit-unified-5h-utilization": "0.5"},
            chunk,
        )
        assert out["model_delivered"] == "claude-opus-4-6"
        assert out["usage"] == {"input_tokens": 1, "output_tokens": 2}
        assert out["rate_limit"]["anthropic-ratelimit-unified-5h-utilization"] == "0.5"

    def test_extract_response_sse_shape(self):
        # First SSE event carries the model_start with the model + usage.
        evt = json.dumps({
            "type": "message_start",
            "message": {
                "model": "claude-haiku-4-5",
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        })
        chunk = f"event: message_start\ndata: {evt}\n\n".encode("utf-8")
        out = extract_response_info({}, chunk)
        assert out["model_delivered"] == "claude-haiku-4-5"
        assert out["usage"] == {"input_tokens": 3, "output_tokens": 4}

    def test_extract_response_synthetic_marker(self):
        chunk = json.dumps({"model": "<synthetic>"}).encode("utf-8")
        out = extract_response_info({}, chunk)
        assert out["synthetic"] is True

    def test_extract_response_non_json(self):
        out = extract_response_info({}, b"<html>error</html>")
        assert out["model_delivered"] is None
        assert out["synthetic"] is False

    def test_rate_limit_no_headers(self):
        out = extract_response_info({}, b'{"ok":1}')
        assert out["rate_limit"] is None


class TestBuildServer:
    def test_raises_when_disabled(self):
        cfg = {"proxy": {"enabled": False}}
        with pytest.raises(RuntimeError, match="not enabled"):
            build_server(cfg)
