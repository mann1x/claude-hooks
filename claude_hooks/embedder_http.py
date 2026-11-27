"""HTTP control endpoint for the daemon-managed embedder (ch-33).

Served by the daemon next to its signed RPC socket, so a session (or a
script, or a gate harness on another tool) can see what the embedder
runs on and move it without the daemon's secret:

  GET  /embedder/status               the backend in force, device, PCI, fallback
  GET  /embedder/backends[?refresh=1] cpu / vulkan / cuda, each device with
                                      id, name, memory and PCI address
  POST /embedder/backend              {"mode": "cpu"|"vulkan"|"cuda",
                                       "device": "...", "pci": "...",
                                       "reason": "...", "persist": true,
                                       "idle_timeout": 120, "force": false}
  POST /embedder/reset                back to the configured backend

A switch waits until the embedder is idle, then restarts it on the new
backend (EmbeddingManager.switch). Bound to loopback by default; on any
other address a non-loopback client gets the GETs only.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("claude_hooks.embedder_http")

MAX_BODY = 64 * 1024


def _loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.split("%")[0]).is_loopback
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    server_version = "claude-hooks-embedder/1"

    def log_message(self, fmt, *args):  # quiet: the daemon log is enough
        log.debug("%s " + fmt, self.client_address[0], *args)

    def _send(self, code: int, obj) -> None:
        body = (json.dumps(obj, indent=1) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @property
    def mgr(self):
        return self.server.manager  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/embedder/status":
                return self._send(200, self.mgr.status())
            if url.path == "/embedder/backends":
                refresh = q.get("refresh", ["0"])[0] not in ("0", "", "false")
                return self._send(200, self.mgr.backends(refresh=refresh))
        except Exception as e:  # noqa: BLE001
            log.exception("embedder GET %s failed", url.path)
            return self._send(500, {"ok": False, "error": str(e)})
        self._send(404, {"ok": False, "error": f"no route {url.path}",
                         "routes": ["GET /embedder/status", "GET /embedder/backends",
                                    "POST /embedder/backend", "POST /embedder/reset"]})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if not _loopback(self.client_address[0]):
            return self._send(403, {"ok": False,
                                    "error": "switching is allowed from loopback only"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            return self._send(400, {"ok": False, "error": "bad Content-Length"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError as e:
            return self._send(400, {"ok": False, "error": f"bad JSON: {e}"})
        if not isinstance(body, dict):
            return self._send(400, {"ok": False, "error": "body must be a JSON object"})
        try:
            if url.path == "/embedder/backend":
                res = self.mgr.switch(
                    str(body.get("mode") or ""), device=str(body.get("device") or ""),
                    pci=str(body.get("pci") or ""), reason=str(body.get("reason") or ""),
                    persist=bool(body.get("persist", True)),
                    idle_timeout=float(body.get("idle_timeout", 120.0)),
                    force=bool(body.get("force", False)))
            elif url.path == "/embedder/reset":
                res = self.mgr.reset(idle_timeout=float(body.get("idle_timeout", 120.0)),
                                     force=bool(body.get("force", False)))
            else:
                return self._send(404, {"ok": False, "error": f"no route {url.path}"})
        except (TypeError, ValueError) as e:
            return self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("embedder POST %s failed", url.path)
            return self._send(500, {"ok": False, "error": str(e)})
        code = 200 if res.get("ok") else (409 if not res.get("changed") else 500)
        self._send(code, res)


class EmbedderHttp(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, manager, host: str, port: int):
        self.manager = manager
        super().__init__((host, port), _Handler)


def start(manager, host: str, port: int) -> Optional[EmbedderHttp]:
    """Serve in a daemon thread; ``None`` (logged) when it cannot bind."""
    if not port:
        return None
    try:
        srv = EmbedderHttp(manager, host, port)
    except OSError as e:
        log.warning("embedder control endpoint not started on %s:%d: %s", host, port, e)
        return None
    threading.Thread(target=srv.serve_forever, name="embedder-http", daemon=True).start()
    log.info("embedder control endpoint on http://%s:%d/embedder/", host, port)
    return srv
