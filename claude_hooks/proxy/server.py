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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from claude_hooks.config import expand_user_path, load_config
from claude_hooks.proxy.forwarder import UpstreamResult, forward
from claude_hooks.proxy.logger import JsonlLogger
from claude_hooks.proxy.metadata import extract_request_info, extract_response_info

log = logging.getLogger("claude_hooks.proxy.server")


def _now_iso() -> str:
    import datetime as _dt
    n = _dt.datetime.utcnow()
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


class _Handler(BaseHTTPRequestHandler):
    # Class-level state injected by ``build_server``.
    proxy_cfg: dict = {}
    jsonl_logger: Optional[JsonlLogger] = None

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
        if self.jsonl_logger is None or not self.proxy_cfg.get("log_requests", True):
            return
        path, _, query = self.path.partition("?")
        record = {
            "ts": _now_iso(),
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
            "usage": resp_meta.get("usage"),
            "rate_limit": resp_meta.get("rate_limit"),
            "is_warmup": req_meta.get("is_warmup", False),
            "synthetic": resp_meta.get("synthetic", False),
            "session_id": req_meta.get("session_id"),
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

    class _HandlerBound(_Handler):
        proxy_cfg = pcfg
    _HandlerBound.jsonl_logger = jsonl_logger

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

    def _stop(_sig: int, _frame) -> None:
        print("\nshutting down…", file=sys.stderr)
        server.shutdown()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        server.serve_forever()
    finally:
        try:
            server.server_close()
        except Exception:
            pass
    return 0
