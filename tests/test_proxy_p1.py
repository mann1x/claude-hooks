"""
Phase-P1 tests: SSE tailing + rate-limit state file + weekly-script
auto-populate.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from claude_hooks.proxy.server import build_server
from claude_hooks.proxy.sse import SseTail, merge_usage
from claude_hooks.proxy.ratelimit_state import (
    read_state_file, update_state_file,
)


# ============================================================ #
# Unit: SSE tail
# ============================================================ #
class TestSseTail:
    def test_parses_message_start(self):
        tail = SseTail()
        evt = json.dumps({
            "type": "message_start",
            "message": {
                "model": "claude-opus-4-6",
                "usage": {"input_tokens": 5, "output_tokens": 0,
                          "cache_read_input_tokens": 100},
            },
        })
        chunk = f"event: message_start\ndata: {evt}\n\n".encode("utf-8")
        list(tail.wrap([chunk]))
        assert tail.final_usage == {
            "input_tokens": 5, "output_tokens": 0,
            "cache_read_input_tokens": 100,
        }

    def test_parses_message_delta_overwrites_start(self):
        tail = SseTail()
        start = json.dumps({
            "type": "message_start",
            "message": {"model": "x",
                        "usage": {"input_tokens": 5, "output_tokens": 0}},
        })
        delta = json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"input_tokens": 5, "output_tokens": 42},
        })
        chunks = [
            f"event: message_start\ndata: {start}\n\n".encode("utf-8"),
            f"event: message_delta\ndata: {delta}\n\n".encode("utf-8"),
        ]
        list(tail.wrap(chunks))
        assert tail.final_usage["output_tokens"] == 42
        assert tail.stop_reason == "end_turn"

    def test_handles_split_events(self):
        """Event split across two chunks arrives intact."""
        tail = SseTail()
        delta = json.dumps({
            "type": "message_delta",
            "usage": {"output_tokens": 99},
        })
        full = f"event: message_delta\ndata: {delta}\n\n".encode("utf-8")
        # Split in the middle of the JSON.
        half = len(full) // 2
        list(tail.wrap([full[:half], full[half:]]))
        assert tail.final_usage == {"output_tokens": 99}

    def test_passthrough_preserves_bytes(self):
        """Bytes going to the consumer are identical to input."""
        tail = SseTail()
        payload = b"event: message_delta\ndata: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":1}}\n\n"
        out = b"".join(tail.wrap([payload]))
        assert out == payload

    def test_non_sse_bytes_dont_crash(self):
        tail = SseTail()
        list(tail.wrap([b"<html>not sse</html>"]))
        assert tail.final_usage is None
        assert tail.stop_reason is None

    def test_malformed_json_skipped(self):
        tail = SseTail()
        chunk = b"event: message_delta\ndata: not json{\n\n"
        list(tail.wrap([chunk]))
        assert tail.final_usage is None

    def test_event_count_tracked(self):
        tail = SseTail()
        chunks = []
        for _ in range(3):
            d = json.dumps({"type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": "x"}})
            chunks.append(f"event: content_block_delta\ndata: {d}\n\n".encode())
        list(tail.wrap(chunks))
        assert tail.event_counts["content_block_delta"] == 3


class TestMergeUsage:
    def test_delta_overrides_start(self):
        out = merge_usage(
            {"input_tokens": 5, "output_tokens": 0},
            {"input_tokens": 5, "output_tokens": 42},
        )
        assert out["output_tokens"] == 42

    def test_both_none(self):
        assert merge_usage(None, None) is None

    def test_only_start(self):
        assert merge_usage({"input_tokens": 3}, None) == {"input_tokens": 3}


# ============================================================ #
# Unit: rate-limit state file
# ============================================================ #
class TestRateLimitStateFile:
    def test_writes_5h_utilization(self, tmp_path):
        p = tmp_path / "state.json"
        update_state_file(
            p,
            rate_limit_headers={
                "anthropic-ratelimit-unified-5h-utilization": "0.42",
                "anthropic-ratelimit-unified-representative-claim": "five_hour",
            },
            request_ts="2026-04-14T15:00:00Z",
        )
        s = read_state_file(p)
        assert s["five_hour_utilization"] == 0.42
        assert s["five_hour_remaining"] == pytest.approx(0.58)
        assert s["representative_claim"] == "five_hour"

    def test_writes_both_windows(self, tmp_path):
        p = tmp_path / "state.json"
        update_state_file(
            p,
            rate_limit_headers={
                "anthropic-ratelimit-unified-5h-utilization": "0.42",
                "anthropic-ratelimit-unified-7d-utilization": "0.18",
            },
            request_ts="2026-04-14T15:00:00Z",
        )
        s = read_state_file(p)
        assert s["five_hour_utilization"] == 0.42
        assert s["seven_day_utilization"] == 0.18

    def test_skips_write_when_no_recognised_headers(self, tmp_path):
        p = tmp_path / "state.json"
        update_state_file(
            p,
            rate_limit_headers={"x-other": "1"},
            request_ts="2026-04-14T15:00:00Z",
        )
        assert not p.exists()

    def test_none_headers_noop(self, tmp_path):
        p = tmp_path / "state.json"
        update_state_file(p, rate_limit_headers=None,
                          request_ts="2026-04-14T15:00:00Z")
        assert not p.exists()

    def test_atomic_replace(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text('{"five_hour_utilization": 0.1}')
        update_state_file(
            p,
            rate_limit_headers={
                "anthropic-ratelimit-unified-5h-utilization": "0.99",
            },
            request_ts="2026-04-14T15:00:00Z",
        )
        s = read_state_file(p)
        assert s["five_hour_utilization"] == 0.99

    def test_read_missing_returns_none(self, tmp_path):
        assert read_state_file(tmp_path / "missing.json") is None

    def test_read_corrupt_returns_none(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("not json")
        assert read_state_file(p) is None


# ============================================================ #
# Integration: proxy -> SSE upstream -> JSONL + state file
# ============================================================ #
def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _SseUpstream(BaseHTTPRequestHandler):
    captured: list = []
    events: list[bytes] = []
    response_headers_dict: dict = {}

    def log_message(self, *a, **kw): pass

    def do_POST(self):
        clen = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(clen)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        for k, v in self.response_headers_dict.items():
            self.send_header(k, v)
        self.end_headers()
        for evt in self.events:
            self.wfile.write(evt)
            self.wfile.flush()

    do_GET = do_POST


@pytest.fixture
def sse_upstream():
    _SseUpstream.captured = []
    _SseUpstream.events = []
    _SseUpstream.response_headers_dict = {}
    port = _find_free_port()
    srv = HTTPServer(("127.0.0.1", port), _SseUpstream)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", _SseUpstream
    finally:
        srv.shutdown()
        srv.server_close()


class TestProxySseIntegration:
    def test_final_usage_from_message_delta(self, sse_upstream, tmp_path):
        url, h = sse_upstream
        start = json.dumps({
            "type": "message_start",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 10, "output_tokens": 0}},
        })
        delta = json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"input_tokens": 10, "output_tokens": 55},
        })
        stop = json.dumps({"type": "message_stop"})
        h.events = [
            f"event: message_start\ndata: {start}\n\n".encode(),
            f"event: message_delta\ndata: {delta}\n\n".encode(),
            f"event: message_stop\ndata: {stop}\n\n".encode(),
        ]
        h.response_headers_dict = {
            "anthropic-ratelimit-unified-5h-utilization": "0.37",
            "anthropic-ratelimit-unified-representative-claim": "five_hour",
        }
        cfg = {"proxy": {
            "enabled": True,
            "listen_host": "127.0.0.1",
            "listen_port": _find_free_port(),
            "upstream": url,
            "timeout": 5.0,
            "log_requests": True,
            "log_dir": str(tmp_path / "log"),
            "log_retention_days": 1,
            "record_rate_limit_headers": True,
            "block_warmup": False,
        }}
        server, _ = build_server(cfg)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        host, port = server.server_address
        try:
            req = urllib.request.Request(
                f"http://{host}:{port}/v1/messages",
                data=json.dumps({
                    "model": "claude-opus-4-6",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            body = resp.read()
            assert b"message_delta" in body
            assert b"message_stop" in body
        finally:
            server.shutdown(); server.server_close()
        time.sleep(0.1)
        # Check the JSONL log has the delta's final usage, not message_start's.
        lines = list((tmp_path / "log").glob("*.jsonl"))
        assert len(lines) == 1
        rec = json.loads(lines[0].read_text().strip())
        assert rec["usage"]["output_tokens"] == 55   # final_delta wins
        assert rec["stop_reason"] == "end_turn"
        # Rate-limit state file written.
        state = read_state_file(tmp_path / "log" / "ratelimit-state.json")
        assert state["five_hour_utilization"] == 0.37
        assert state["representative_claim"] == "five_hour"


# ============================================================ #
# Integration: weekly_token_usage.py auto-populate
# ============================================================ #
class TestWeeklyScriptAutoPopulate:
    def test_reads_proxy_state_when_flag_absent(self, tmp_path):
        state_dir = tmp_path / "proxy"
        state_dir.mkdir()
        state_file = state_dir / "ratelimit-state.json"
        state_file.write_text(json.dumps({
            "last_updated": "2026-04-14T15:00:00Z",
            "source_request_ts": "2026-04-14T14:59:58.123Z",
            "representative_claim": "five_hour",
            "five_hour_utilization": 0.73,
            "five_hour_remaining": 0.27,
            "raw_headers": {},
        }))

        script = Path(__file__).resolve().parent.parent / "scripts" / "weekly_token_usage.py"
        out = subprocess.run(
            [sys.executable, str(script),
             "--proxy-state", str(state_file),
             # Point at an empty projects dir so no transcripts skew the output.
             "--projects-dir", str(tmp_path / "empty-projects")],
            capture_output=True, text=True, timeout=15,
        )
        assert out.returncode == 0, out.stderr
        # Auto-populate banner should appear at the bottom.
        assert "auto-populated from claude-hooks proxy" in out.stdout
        # %Limit column present (since we auto-filled current_usage_pct)
        assert "%Limit" in out.stdout

    def test_proxy_log_warmup_stats_in_output(self, tmp_path):
        # Create a proxy log with a mix of warmup_blocked, warmup
        # passed through, synthetic RL, and ordinary requests.
        import datetime as _dt
        proxy_log = tmp_path / "proxy-log"
        proxy_log.mkdir()
        today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        now_iso = _dt.datetime.utcnow().isoformat() + "Z"
        lines = [
            {"ts": now_iso, "method": "POST", "path": "/v1/messages",
             "status": 200, "warmup_blocked": True, "is_warmup": True},
            {"ts": now_iso, "method": "POST", "path": "/v1/messages",
             "status": 200, "warmup_blocked": True, "is_warmup": True},
            {"ts": now_iso, "method": "POST", "path": "/v1/messages",
             "status": 200, "is_warmup": True},                  # passed through
            {"ts": now_iso, "method": "POST", "path": "/v1/messages",
             "status": 200, "synthetic": True},
            {"ts": now_iso, "method": "POST", "path": "/v1/messages",
             "status": 200},
        ]
        (proxy_log / f"{today}.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n"
        )
        script = Path(__file__).resolve().parent.parent / "scripts" / "weekly_token_usage.py"
        out = subprocess.run(
            [sys.executable, str(script),
             "--proxy-log-dir", str(proxy_log),
             "--projects-dir", str(tmp_path / "empty")],
            capture_output=True, text=True, timeout=15,
        )
        assert out.returncode == 0
        assert "Proxy this week" in out.stdout
        assert "2 Warmup(s) BLOCKED" in out.stdout
        assert "1 Warmup(s) passed" in out.stdout
        assert "1 synthetic rate-limit" in out.stdout

    def test_proxy_log_missing_dir_no_footer(self, tmp_path):
        script = Path(__file__).resolve().parent.parent / "scripts" / "weekly_token_usage.py"
        out = subprocess.run(
            [sys.executable, str(script),
             "--proxy-log-dir", str(tmp_path / "nope"),
             "--projects-dir", str(tmp_path / "empty")],
            capture_output=True, text=True, timeout=15,
        )
        assert out.returncode == 0
        assert "Proxy this week" not in out.stdout

    def test_no_autopopulate_when_flag_passed(self, tmp_path):
        state_file = tmp_path / "ratelimit-state.json"
        state_file.write_text(json.dumps({
            "five_hour_utilization": 0.73,
            "representative_claim": "five_hour",
        }))
        script = Path(__file__).resolve().parent.parent / "scripts" / "weekly_token_usage.py"
        out = subprocess.run(
            [sys.executable, str(script),
             "--current-usage-pct", "50",
             "--proxy-state", str(state_file),
             "--projects-dir", str(tmp_path / "empty")],
            capture_output=True, text=True, timeout=15,
        )
        assert out.returncode == 0
        # The auto-populate banner should NOT appear because user supplied --current-usage-pct.
        assert "auto-populated" not in out.stdout
