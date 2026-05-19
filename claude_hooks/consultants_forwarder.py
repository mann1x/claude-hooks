"""Smart-start forwarder for the /consultants engine.

Sits in front of the engine and lazily spawns it as a subprocess on
the first request. Tracks idle time; reaps the engine after
``idle_timeout_seconds`` of no traffic, then respawns it on the
next request. Listens on ``hooks.consultants.smart_start.forwarder_url``
(default ``http://127.0.0.1:38096``).

This module is **stdlib-only**. The engine subprocess it spawns
runs in the dedicated ``claude-hooks-consultants`` conda env; the
forwarder itself just needs ``http.server`` and ``urllib`` to
proxy traffic. That means the daemon — which already runs in the
main ``claude-hooks`` env — can host this forwarder without
pulling in LangChain.

Lifecycle:

1. Constructor takes a ``ForwarderConfig`` (engine python, port,
   timeouts).
2. ``serve_forever()`` starts the HTTP listener AND a background
   idle-reaper thread.
3. On each request: check ``_engine_alive()``; if not, spawn it
   (waiting for ``/v1/health`` to return 200, up to
   ``spawn_timeout_seconds``); forward the request body via
   ``urllib`` to the engine; update ``last_activity_at``.
4. Reaper thread wakes every ``reaper_interval_seconds``; if
   ``now - last_activity_at > idle_timeout_seconds`` and the
   engine is alive, sends SIGTERM, waits 10 s, then SIGKILL,
   clears the pid.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

log = logging.getLogger("claude_hooks.consultants_forwarder")


# ----------------------- config --------------------------------- #

@dataclass
class ForwarderConfig:
    """All knobs the forwarder needs. Resolved by the caller from
    ``config/claude-hooks.json`` + the consultants conda env path."""

    listen_host: str = "127.0.0.1"
    listen_port: int = 38096
    engine_host: str = "127.0.0.1"
    engine_port: int = 0  # 0 = pick an ephemeral port at spawn time
    engine_python: str = ""  # absolute path to consultants env python
    engine_module: str = "consultants.server"
    extra_engine_args: list[str] = field(default_factory=list)
    repo_root: str = ""  # cwd for the engine subprocess
    spawn_timeout_seconds: float = 15.0
    idle_timeout_seconds: float = 1800.0
    reaper_interval_seconds: float = 60.0
    request_timeout_seconds: float = 600.0


# ----------------------- engine manager ------------------------- #

class EngineManager:
    """Owns the engine subprocess. Thread-safe.

    Single source of truth for ``self.proc`` and
    ``self.last_activity_at``. All access goes through ``self.lock``.
    """

    def __init__(self, cfg: ForwarderConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.engine_port: int = cfg.engine_port or 0
        self.last_activity_at: float = time.time()
        self._stop_reaper = threading.Event()

    # ----- spawn / reap ----- #

    def _pick_port(self) -> int:
        """Pick an ephemeral port if the configured one is 0. Race
        window is small (someone else can grab the port between bind
        + close + spawn), but acceptable for a localhost-only
        forwarder. ``engine_port`` set explicitly bypasses this."""
        if self.cfg.engine_port:
            return self.cfg.engine_port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _engine_alive(self) -> bool:
        if self.proc is None:
            return False
        if self.proc.poll() is not None:
            log.info("engine subprocess exited (rc=%s); clearing",
                     self.proc.returncode)
            self.proc = None
            return False
        return True

    def _wait_for_health(self, port: int) -> bool:
        deadline = time.monotonic() + self.cfg.spawn_timeout_seconds
        url = f"http://{self.cfg.engine_host}:{port}/v1/health"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1.0) as r:
                    if r.status == 200:
                        return True
            except urllib.error.URLError:
                pass
            time.sleep(0.2)
        return False

    def ensure_running(self) -> int:
        """Spawn the engine if it isn't already. Returns the engine
        port. Raises RuntimeError on failure."""
        with self.lock:
            if self._engine_alive():
                self.last_activity_at = time.time()
                return self.engine_port

            if not self.cfg.engine_python or not os.path.isfile(
                    self.cfg.engine_python):
                raise RuntimeError(
                    f"engine_python not found: {self.cfg.engine_python!r}. "
                    "Run `python install.py` to create the "
                    "claude-hooks-consultants conda env."
                )

            port = self._pick_port()
            cmd = [
                self.cfg.engine_python, "-m", self.cfg.engine_module,
                "--host", self.cfg.engine_host,
                "--port", str(port),
                *self.cfg.extra_engine_args,
            ]
            log.info("spawning engine: %s", " ".join(cmd))
            # Windows: pythonw.exe alone is windowless, but if the
            # registered --engine-python points at python.exe (e.g.
            # pre-v1.8.1 install.py where consultants_py was passed
            # instead of pyw), a visible console pops up on the user's
            # desktop. CREATE_NO_WINDOW | DETACHED_PROCESS makes the
            # spawn windowless regardless of which exe is used —
            # mirroring chat_model_manager / embedding_manager /
            # lsp_engine.client which already guard their spawns.
            # POSIX: start_new_session=True suffices.
            popen_kwargs: dict = dict(
                cwd=self.cfg.repo_root or None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if os.name == "nt":
                popen_kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                )
            else:
                popen_kwargs["start_new_session"] = True
            try:
                self.proc = subprocess.Popen(cmd, **popen_kwargs)
            except OSError as e:
                raise RuntimeError(f"engine spawn failed: {e}") from e

            if not self._wait_for_health(port):
                # Spawn succeeded but engine never came up healthy.
                self._terminate_locked()
                raise RuntimeError(
                    f"engine did not respond on port {port} within "
                    f"{self.cfg.spawn_timeout_seconds}s"
                )
            self.engine_port = port
            self.last_activity_at = time.time()
            log.info("engine ready on port %d (pid=%s)",
                     port, self.proc.pid)
            return port

    def touch(self) -> None:
        """Refresh idle clock. Called after every successful
        forwarded request."""
        self.last_activity_at = time.time()

    def _terminate_locked(self, *, grace_seconds: float = 10.0) -> None:
        """SIGTERM, then SIGKILL after grace. Caller holds self.lock."""
        if self.proc is None:
            return
        try:
            log.info("terminating engine pid=%s (SIGTERM)", self.proc.pid)
            self.proc.terminate()
        except OSError as e:
            log.warning("SIGTERM failed: %s", e)
        try:
            self.proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            log.warning("engine did not stop after %.1fs; SIGKILL",
                        grace_seconds)
            try:
                self.proc.kill()
                self.proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.error("SIGKILL failed: %s", e)
        self.proc = None
        self.engine_port = 0

    def maybe_reap(self) -> bool:
        """Called by the reaper thread. Returns True if a reap
        happened. Threadsafe."""
        with self.lock:
            if self.proc is None:
                return False
            idle = time.time() - self.last_activity_at
            if idle < self.cfg.idle_timeout_seconds:
                return False
            log.info("reaper: engine idle %.1fs > %.1fs; reaping",
                     idle, self.cfg.idle_timeout_seconds)
            self._terminate_locked()
            return True

    def shutdown(self) -> None:
        self._stop_reaper.set()
        with self.lock:
            self._terminate_locked()


# ----------------------- HTTP handler --------------------------- #

class _ForwardingHandler(BaseHTTPRequestHandler):
    """Stdlib HTTP handler that proxies to the engine. The
    EngineManager is attached via ``server.manager``."""

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _proxy(self) -> None:
        manager: EngineManager = self.server.manager  # type: ignore[attr-defined]
        try:
            port = manager.ensure_running()
        except RuntimeError as e:
            self._send_json(503, {"detail": str(e)})
            return

        # Read the request body (if any).
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        upstream = f"http://{manager.cfg.engine_host}:{port}{self.path}"
        req = urllib.request.Request(
            upstream, data=body, method=self.command,
            headers={k: v for k, v in self.headers.items()
                     if k.lower() not in {"host", "connection",
                                           "content-length"}},
        )
        if body is not None:
            req.add_header("Content-Length", str(len(body)))

        try:
            with urllib.request.urlopen(
                    req, timeout=manager.cfg.request_timeout_seconds) as r:
                payload = r.read()
                status = r.status
                # Pass through interesting headers; skip hop-by-hop.
                ct = r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            payload = e.read() if hasattr(e, "read") else b""
            status = e.code
            ct = e.headers.get("Content-Type", "application/json") \
                if e.headers else "application/json"
        except urllib.error.URLError as e:
            self._send_json(502, {"detail": f"engine unreachable: {e.reason}"})
            return

        manager.touch()
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _proxy
    do_POST = _proxy
    do_DELETE = _proxy

    def _send_json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


# ----------------------- forwarder server ----------------------- #

class Forwarder:
    """Top-level smart-start forwarder. Owns the EngineManager, the
    HTTP server, and the reaper thread."""

    def __init__(self, cfg: ForwarderConfig):
        self.cfg = cfg
        self.manager = EngineManager(cfg)
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._reaper_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.httpd = ThreadingHTTPServer(
            (self.cfg.listen_host, self.cfg.listen_port),
            _ForwardingHandler,
        )
        # Attach the manager so the handler can find it.
        self.httpd.manager = self.manager  # type: ignore[attr-defined]
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop,
            name="consultants-reaper",
            daemon=True,
        )
        self._reaper_thread.start()
        log.info("forwarder listening on %s:%d (engine_python=%s)",
                 self.cfg.listen_host, self.cfg.listen_port,
                 self.cfg.engine_python)

    def serve_forever(self) -> None:
        if self.httpd is None:
            self.start()
        assert self.httpd is not None
        try:
            self.httpd.serve_forever()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        log.info("shutting down forwarder")
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        self.manager.shutdown()

    def _reaper_loop(self) -> None:
        while not self.manager._stop_reaper.is_set():
            time.sleep(self.cfg.reaper_interval_seconds)
            try:
                self.manager.maybe_reap()
            except Exception as e:  # pragma: no cover
                log.warning("reaper error: %s", e)


# ----------------------- entry point ---------------------------- #

def _resolve_engine_python() -> str:
    """Locate the claude-hooks-consultants conda env's python.
    Mirrors bin/_resolve_python_consultants.sh."""
    home = os.environ.get("HOME", "")
    user = os.environ.get("USERPROFILE", home)
    pinned = os.environ.get("CLAUDE_CONSULTANTS_PY")
    if pinned and os.path.isfile(pinned):
        return pinned
    candidates = [
        f"{home}/anaconda3/envs/claude-hooks-consultants/bin/python",
        f"{home}/miniconda3/envs/claude-hooks-consultants/bin/python",
        f"{user}/Anaconda3/envs/claude-hooks-consultants/python.exe",
        f"{user}/Miniconda3/envs/claude-hooks-consultants/python.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="consultants-forwarder",
        description="Smart-start forwarder for the /consultants engine.",
    )
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=38096)
    parser.add_argument("--engine-host", default="127.0.0.1")
    parser.add_argument("--engine-port", type=int, default=0,
                        help="0 = pick ephemeral port per spawn.")
    parser.add_argument("--engine-python",
                        default=_resolve_engine_python(),
                        help="claude-hooks-consultants env's python.")
    parser.add_argument("--repo-root",
                        default=os.path.dirname(
                            os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--idle-timeout", type=float, default=1800.0)
    parser.add_argument("--reaper-interval", type=float, default=60.0)
    parser.add_argument("--spawn-timeout", type=float, default=15.0)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not args.engine_python:
        print("error: claude-hooks-consultants env not found. "
              "Run `python install.py`.", file=sys.stderr)
        return 2

    cfg = ForwarderConfig(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        engine_host=args.engine_host,
        engine_port=args.engine_port,
        engine_python=args.engine_python,
        repo_root=args.repo_root,
        idle_timeout_seconds=args.idle_timeout,
        reaper_interval_seconds=args.reaper_interval,
        spawn_timeout_seconds=args.spawn_timeout,
    )
    fw = Forwarder(cfg)
    try:
        fw.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
