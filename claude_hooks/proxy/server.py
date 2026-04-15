"""
The proxy HTTP server.

Threaded; one thread per concurrent client. Each request:

1. Read the inbound body
2. Extract request metadata (model requested, Warmup detection, ...)
3. Forward upstream via ``forwarder.forward``
4. Extract response metadata from the first chunk + headers
5. Stream the upstream body back to the client
6. Append a JSONL record

The server is safe to ``Ctrl-C`` at any time — connections drop, no
state is persisted beyond the already-written JSONL lines.
"""

from __future__ import annotations

import logging
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from claude_hooks.config import expand_user_path, load_config
from claude_hooks.proxy.forwarder import UpstreamResult, forward
from claude_hooks.proxy.logger import JsonlLogger
from claude_hooks.proxy.metadata import extract_request_info, extract_response_info
from claude_hooks.proxy.ratelimit_state import update_state_file
from claude_hooks.proxy.stub import build_non_streaming, build_streaming

log = logging.getLogger("claude_hooks.proxy.server")


def _now_iso() -> str:
    import datetime as _dt
    n = _dt.datetime.utcnow()
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------- #
# Debug: one-shot request-body dumper.
# ---------------------------------------------------------------------- #
# Set CLAUDE_HOOKS_PROXY_DUMP_DIR=/some/path to capture the next
# CLAUDE_HOOKS_PROXY_DUMP_COUNT (default 3) POST /v1/messages bodies
# to that directory as {ts}-{session}.json. Used when designing S2
# (request-body parser extensions) without guessing the schema.
# After the quota is hit further requests are ignored.
import os
import threading
_DUMP_LOCK = threading.Lock()
_DUMP_REMAINING = int(os.environ.get("CLAUDE_HOOKS_PROXY_DUMP_COUNT", "3"))


def _maybe_dump_request(handler, body: bytes, req_meta: dict) -> None:
    dump_dir = os.environ.get("CLAUDE_HOOKS_PROXY_DUMP_DIR")
    if not dump_dir or not body:
        return
    path_only = handler.path.split("?", 1)[0]
    if path_only != "/v1/messages":
        return
    global _DUMP_REMAINING
    with _DUMP_LOCK:
        if _DUMP_REMAINING <= 0:
            return
        _DUMP_REMAINING -= 1
    try:
        Path(dump_dir).mkdir(parents=True, exist_ok=True)
        ts = _now_iso().replace(":", "-")
        name = f"{ts}-{'warmup' if req_meta.get('is_warmup') else 'main'}.json"
        Path(dump_dir, name).write_bytes(body)
        log.info("dumped request body to %s/%s (%d bytes)",
                 dump_dir, name, len(body))
    except OSError as e:
        log.debug("dump failed: %s", e)


class _Handler(BaseHTTPRequestHandler):
    # Class-level state injected by ``build_server``.
    proxy_cfg: dict = {}
    jsonl_logger: Optional[JsonlLogger] = None
    ratelimit_state_path: Optional[Path] = None

    # Silence default access log — we have our own.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("%s - - %s", self.address_string(), format % args)

    # All HTTP verbs go through the same path.
    def do_GET(self): self._handle()
    def do_POST(self): self._handle()
    def do_PUT(self): self._handle()
    def do_DELETE(self): self._handle()
    def do_PATCH(self): self._handle()
    def do_OPTIONS(self): self._handle()
    def do_HEAD(self): self._handle()

    def _handle(self) -> None:
        started = time.time()
        cfg = self.proxy_cfg
        upstream = cfg.get("upstream", "https://api.anthropic.com")
        timeout = float(cfg.get("timeout", 120.0))

        # --- Read inbound body
        body = b""
        clen = self.headers.get("Content-Length")
        if clen is not None:
            try:
                n = int(clen)
                if n > 0:
                    body = self.rfile.read(n)
            except (ValueError, OSError):
                body = b""

        req_meta = extract_request_info(
            body, {k: v for k, v in self.headers.items()}
        )

        # --- Debug: dump the next N request bodies, then disable.
        # Controlled by CLAUDE_HOOKS_PROXY_DUMP_DIR. Bounded via a
        # class-level counter so we never dump more than N per process.
        _maybe_dump_request(self, body, req_meta)

        # --- P3: short-circuit Warmup when block_warmup is enabled
        if req_meta.get("is_warmup") and cfg.get("block_warmup", False):
            self._send_warmup_stub(req_meta, started, len(body))
            return

        # --- Forward upstream
        try:
            result: UpstreamResult = forward(
                upstream,
                self.command,
                self.path,
                {k: v for k, v in self.headers.items()},
                body,
                timeout=timeout,
            )
        except Exception as e:
            log.warning(
                "upstream call failed: %s %s -> %s",
                self.command, self.path, e,
            )
            self._send_bad_gateway(str(e), started, req_meta, len(body))
            return

        resp_meta = extract_response_info(result.headers, result.first_chunk)

        # --- Mirror status + headers to client
        try:
            self.send_response(result.status, result.reason or None)
            for k, v in result.headers.items():
                self.send_header(k, v)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            log.debug("client dropped before headers")
            self._log_line(started, req_meta, resp_meta, result, len(body),
                           extra={"error": "client_dropped_before_headers"})
            return

        # --- Stream the body
        total_out = 0
        try:
            if result.first_chunk:
                self.wfile.write(result.first_chunk)
                total_out += len(result.first_chunk)
            for chunk in result.body_iter:
                if not chunk:
                    continue
                self.wfile.write(chunk)
                total_out += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            log.debug("client dropped mid-stream")

        self._log_line(
            started, req_meta, resp_meta, result,
            req_bytes=len(body), resp_bytes=total_out,
        )

    # -------------------------------------------------------------- #
    def _send_warmup_stub(
        self, req_meta: dict, started: float, req_bytes: int,
    ) -> None:
        """Short-circuit a blocked Warmup request without touching upstream.

        P3 feature. Returns an Anthropic-compatible minimal reply
        (JSON or SSE depending on ``req_meta['stream']``) so Claude
        Code's warmup priming succeeds with 0 upstream tokens charged.
        """
        import uuid
        msg_id = f"msg_warmup_{uuid.uuid4().hex[:16]}"
        streaming = bool(req_meta.get("stream"))
        model = req_meta.get("model_requested")
        if streaming:
            status, hdrs, body = build_streaming(model, msg_id)
        else:
            status, hdrs, body = build_non_streaming(model, msg_id)
        try:
            self.send_response(status)
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            log.debug("client dropped during warmup stub")
        # Log with blocked=True so the dashboard can count savings.
        self._log_line(
            started, req_meta,
            {"model_delivered": model, "usage": None,
             "rate_limit": None, "synthetic": False},
            None,
            req_bytes=req_bytes, resp_bytes=len(body),
            extra={"status": status, "warmup_blocked": True,
                   "stub_kind": "sse" if streaming else "json"},
        )

    def _send_bad_gateway(
        self, msg: str, started: float, req_meta: dict, req_bytes: int,
    ) -> None:
        body = f'{{"error":{{"type":"proxy_error","message":"{msg}"}}}}'.encode("utf-8")
        try:
            self.send_response(502, "Bad Gateway")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass
        self._log_line(
            started, req_meta,
            {"model_delivered": None, "usage": None, "rate_limit": None,
             "synthetic": False},
            None, req_bytes=req_bytes, resp_bytes=len(body),
            extra={"error": "upstream_failure", "detail": msg},
        )

    def _log_line(
        self, started: float, req_meta: dict, resp_meta: dict,
        result: Optional[UpstreamResult],
        req_bytes: int = 0, resp_bytes: int = 0,
        extra: Optional[dict] = None,
    ) -> None:
        # Prefer the SSE-tail's final usage (message_delta) over the
        # first-chunk usage (message_start). The delta carries the
        # canonical billing output_tokens.
        final_usage = resp_meta.get("usage")
        stop_reason = None
        thinking_delta_count = None
        thinking_signature_bytes = None
        thinking_output_tokens = None
        if result is not None and result.sse_tail is not None:
            tail = result.sse_tail
            # Merge: start for input/cache, delta for output.
            merged = dict(resp_meta.get("usage") or {})
            if tail.final_usage:
                merged.update(tail.final_usage)
            if merged:
                final_usage = merged
            stop_reason = tail.stop_reason
            # S3 — thinking metrics. Emit only when a thinking block
            # actually showed up so JSONL lines stay small for
            # thinking-free turns.
            if tail.thinking_delta_count or tail.thinking_signature_bytes:
                thinking_delta_count = tail.thinking_delta_count
                thinking_signature_bytes = tail.thinking_signature_bytes
            if tail.thinking_output_tokens is not None:
                thinking_output_tokens = tail.thinking_output_tokens

        rate_limit = resp_meta.get("rate_limit")
        req_ts = _now_iso()

        # P1 — update rolling rate-limit state file so downstream
        # scripts (weekly_token_usage.py) can read the real %.
        if (
            self.ratelimit_state_path is not None
            and self.proxy_cfg.get("record_rate_limit_headers", True)
        ):
            update_state_file(
                self.ratelimit_state_path,
                rate_limit_headers=rate_limit,
                request_ts=req_ts,
            )

        if self.jsonl_logger is None or not self.proxy_cfg.get("log_requests", True):
            return
        path, _, query = self.path.partition("?")
        record = {
            "ts": req_ts,
            "method": self.command,
            "path": path,
            "query": query,
            "status": result.status if result else 502,
            "duration_ms": int((time.time() - started) * 1000),
            "upstream_host": self.proxy_cfg.get("upstream", ""),
            "req_bytes": req_bytes,
            "resp_bytes": resp_bytes,
            "model_requested": req_meta.get("model_requested"),
            "model_delivered": resp_meta.get("model_delivered"),
            "usage": final_usage,
            "stop_reason": stop_reason,
            "rate_limit": rate_limit,
            "is_warmup": req_meta.get("is_warmup", False),
            "synthetic": resp_meta.get("synthetic", False),
            "session_id": req_meta.get("session_id"),
            # S2 additions — only emitted when non-null to keep lines slim.
            "account_uuid": req_meta.get("account_uuid"),
            "cc_version": req_meta.get("cc_version"),
            "cc_entrypoint": req_meta.get("cc_entrypoint"),
            "effort": req_meta.get("effort"),
            "thinking_type": req_meta.get("thinking_type"),
            "max_tokens": req_meta.get("max_tokens"),
            "num_tools": req_meta.get("num_tools"),
            "num_messages": req_meta.get("num_messages"),
            "agent_type": req_meta.get("agent_type"),
            "agent_name": req_meta.get("agent_name"),
            "request_class": req_meta.get("request_class"),
            "beta_features": req_meta.get("beta_features"),
            "is_sidechain": (
                None if req_meta.get("agent_type") is None
                else req_meta.get("agent_type") != "main"
            ),
            # S3 — thinking-depth proxy. Null when the response had no
            # thinking blocks (keeps JSONL lines compact).
            "thinking_delta_count": thinking_delta_count,
            "thinking_signature_bytes": thinking_signature_bytes,
            "thinking_output_tokens": thinking_output_tokens,
            # Debug: expose the SSE event type histogram so we can see
            # which events actually arrived in the live stream. Cheap
            # (one small dict per response). Can drop later.
            "sse_event_counts": (
                dict(result.sse_tail.event_counts)
                if result is not None and result.sse_tail is not None
                else None
            ),
            "content_block_types": (
                dict(result.sse_tail.content_block_types)
                if result is not None and result.sse_tail is not None
                else None
            ),
            "delta_types": (
                dict(result.sse_tail.delta_types)
                if result is not None and result.sse_tail is not None
                else None
            ),
        }
        if extra:
            record.update(extra)
        self.jsonl_logger.write(record)


# ---------------------------------------------------------------------- #
def build_server(cfg: Optional[dict] = None) -> tuple[ThreadingHTTPServer, JsonlLogger]:
    """Construct but do not start the proxy server."""
    merged = cfg if cfg is not None else load_config()
    pcfg = (merged.get("proxy") or {})
    if not pcfg.get("enabled", False):
        raise RuntimeError(
            "proxy is not enabled. Set hooks.proxy.enabled=true in "
            "config/claude-hooks.json"
        )
    host = pcfg.get("listen_host", "127.0.0.1")
    port = int(pcfg.get("listen_port", 38080))
    log_dir = expand_user_path(pcfg.get("log_dir", "~/.claude/claude-hooks-proxy"))
    retention = int(pcfg.get("log_retention_days", 14))

    jsonl_logger = JsonlLogger(Path(log_dir), retention_days=retention)
    ratelimit_state_path = Path(log_dir) / "ratelimit-state.json"

    class _HandlerBound(_Handler):
        proxy_cfg = pcfg
    _HandlerBound.jsonl_logger = jsonl_logger
    _HandlerBound.ratelimit_state_path = ratelimit_state_path

    server = ThreadingHTTPServer((host, port), _HandlerBound)
    server.daemon_threads = True
    return server, jsonl_logger


def run(cfg: Optional[dict] = None) -> int:
    """CLI entry point. Blocks until ``SIGTERM``/``SIGINT``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        server, jsonl_logger = build_server(cfg)
    except RuntimeError as e:
        print(f"claude-hooks-proxy: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"claude-hooks-proxy: bind failed: {e}", file=sys.stderr)
        return 1

    host, port = server.server_address
    print(
        f"claude-hooks-proxy listening on http://{host}:{port} -> "
        f"{server.RequestHandlerClass.proxy_cfg.get('upstream')}",
        file=sys.stderr,
    )
    print(
        f"  logs: {jsonl_logger.log_dir}",
        file=sys.stderr,
    )
    print(
        f"  set in ~/.claude/settings.json: "
        f'"env": {{"ANTHROPIC_BASE_URL": "http://{host}:{port}"}}',
        file=sys.stderr,
    )

    # Run serve_forever in a background thread so the main thread can
    # receive signals and call server.shutdown() without deadlocking
    # on its own serve loop (which happens when shutdown() is invoked
    # from inside the signal handler on the same thread).
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()

    stop_flag = threading.Event()

    def _stop(_sig: int, _frame) -> None:
        print("\nshutting down…", file=sys.stderr)
        stop_flag.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        while not stop_flag.is_set():
            # Wake up periodically so SIGINT / SIGTERM delivery can be
            # observed without polling tight.
            stop_flag.wait(timeout=1.0)
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        # Daemon threads exit with the process; no need to join.
    return 0
