"""Tests for ``claude_hooks.consultants_forwarder``.

The forwarder spawns a subprocess and proxies HTTP traffic. To
test without pulling in the consultants conda env, we use a tiny
**fake engine** — a Python script that runs an `http.server`
echoing requests + reporting a `/v1/health` route. The forwarder
treats it identically to the real engine.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread

import pytest

from claude_hooks import consultants_forwarder as cf


# ----------------------- fake engine ----------------------------- #

FAKE_ENGINE_SOURCE = textwrap.dedent("""
    import argparse
    import json
    import sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a, **k): pass

        def _send(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/v1/health":
                self._send(200, {"status": "ok"})
            elif self.path.startswith("/echo"):
                self._send(200, {"path": self.path, "method": "GET"})
            else:
                self._send(404, {"detail": "nope"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            self._send(200, {"path": self.path, "method": "POST",
                              "body": body})

    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    args, _ = p.parse_known_args()
    srv = ThreadingHTTPServer((args.host, args.port), H)
    print(f"listening on {args.host}:{args.port}", flush=True)
    srv.serve_forever()
""")


@pytest.fixture
def fake_engine_script(tmp_path: Path) -> Path:
    p = tmp_path / "fake_engine.py"
    p.write_text(FAKE_ENGINE_SOURCE)
    return p


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_cfg(fake_engine: Path, *, idle: float = 1800.0,
              reap_interval: float = 0.1) -> cf.ForwarderConfig:
    return cf.ForwarderConfig(
        listen_host="127.0.0.1",
        listen_port=_free_port(),
        engine_python=sys.executable,
        engine_module="",  # we'll override engine_module below
        engine_port=0,
        idle_timeout_seconds=idle,
        reaper_interval_seconds=reap_interval,
        spawn_timeout_seconds=5.0,
    )


# ----------------------- EngineManager --------------------------- #

class TestEngineManager:
    def test_spawn_and_health(self, fake_engine_script):
        cfg = _make_cfg(fake_engine_script)
        # Hack: replace engine_module mechanism with explicit args.
        # The forwarder builds [python, -m, module]. We swap to
        # [python, script_path] via extra_engine_args + a custom path.
        cfg.engine_python = sys.executable
        # Manually drive: subprocess directly via the spawn path.
        # Simpler: reach into the manager and override the cmd.
        mgr = cf.EngineManager(cfg)
        # Patch ensure_running's command construction by overriding
        # _pick_port + monkey-patching subprocess.Popen call. Simplest:
        # override engine_module to a sentinel and patch _spawn impl.
        orig_ensure = mgr.ensure_running

        def patched_ensure():
            with mgr.lock:
                if mgr._engine_alive():
                    mgr.last_activity_at = time.time()
                    return mgr.engine_port
                port = mgr._pick_port()
                mgr.proc = subprocess.Popen(
                    [sys.executable, str(fake_engine_script),
                     "--port", str(port)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if not mgr._wait_for_health(port):
                    mgr._terminate_locked()
                    raise RuntimeError("fake engine never came up")
                mgr.engine_port = port
                mgr.last_activity_at = time.time()
                return port

        mgr.ensure_running = patched_ensure  # type: ignore
        try:
            port = mgr.ensure_running()
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/health") as r:
                assert r.status == 200
            assert mgr._engine_alive()
        finally:
            mgr.shutdown()
            assert not mgr._engine_alive()

    def test_idempotent_spawn(self, fake_engine_script):
        # Second ensure_running with engine alive returns same port,
        # doesn't spawn a new process.
        cfg = _make_cfg(fake_engine_script)
        mgr = cf.EngineManager(cfg)
        try:
            mgr.proc = subprocess.Popen(
                [sys.executable, str(fake_engine_script),
                 "--port", str(_free_port())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.3)
            mgr.engine_port = 12345  # arbitrary
            mgr.last_activity_at = time.time()
            first_pid = mgr.proc.pid
            # ensure_running detects alive proc, returns engine_port
            assert mgr.ensure_running() == 12345
            assert mgr.proc.pid == first_pid
        finally:
            mgr.shutdown()

    def test_missing_engine_python_raises(self):
        cfg = cf.ForwarderConfig(engine_python="/nope/python")
        mgr = cf.EngineManager(cfg)
        with pytest.raises(RuntimeError, match="engine_python not found"):
            mgr.ensure_running()

    def test_terminate(self, fake_engine_script):
        cfg = _make_cfg(fake_engine_script)
        mgr = cf.EngineManager(cfg)
        mgr.proc = subprocess.Popen(
            [sys.executable, str(fake_engine_script),
             "--port", str(_free_port())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.2)
        with mgr.lock:
            mgr._terminate_locked(grace_seconds=2.0)
        assert mgr.proc is None

    def test_maybe_reap_under_threshold(self, fake_engine_script):
        cfg = _make_cfg(fake_engine_script, idle=10.0)
        mgr = cf.EngineManager(cfg)
        mgr.proc = subprocess.Popen(
            [sys.executable, str(fake_engine_script),
             "--port", str(_free_port())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.2)
        mgr.last_activity_at = time.time()  # just touched
        try:
            assert mgr.maybe_reap() is False  # not stale
            assert mgr.proc is not None
        finally:
            mgr.shutdown()

    def test_maybe_reap_past_threshold(self, fake_engine_script):
        cfg = _make_cfg(fake_engine_script, idle=0.5)
        mgr = cf.EngineManager(cfg)
        mgr.proc = subprocess.Popen(
            [sys.executable, str(fake_engine_script),
             "--port", str(_free_port())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.2)
        mgr.last_activity_at = time.time() - 5.0  # 5s ago, idle=0.5
        assert mgr.maybe_reap() is True
        assert mgr.proc is None


# ----------------------- Forwarder integration ------------------ #

class _ForwarderHarness:
    """Spin up a real Forwarder pointed at the fake engine."""
    def __init__(self, fake_engine_script: Path, *,
                 idle: float = 1800.0, reap_interval: float = 0.1):
        self.cfg = _make_cfg(fake_engine_script,
                             idle=idle, reap_interval=reap_interval)
        self.fake_engine = fake_engine_script
        self.fw = cf.Forwarder(self.cfg)
        # Override EngineManager.ensure_running to spawn the fake.
        mgr = self.fw.manager
        orig_ensure = mgr.ensure_running

        def patched():
            with mgr.lock:
                if mgr._engine_alive():
                    mgr.last_activity_at = time.time()
                    return mgr.engine_port
                port = mgr._pick_port()
                mgr.proc = subprocess.Popen(
                    [sys.executable, str(self.fake_engine),
                     "--port", str(port)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if not mgr._wait_for_health(port):
                    mgr._terminate_locked()
                    raise RuntimeError("fake engine spawn failed")
                mgr.engine_port = port
                mgr.last_activity_at = time.time()
                return port

        mgr.ensure_running = patched  # type: ignore
        self._thread: Thread | None = None

    def __enter__(self):
        self.fw.start()
        self._thread = Thread(target=self.fw.httpd.serve_forever,
                              daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *a):
        self.fw.shutdown()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.listen_port}"


class TestForwarder:
    def test_first_request_spawns_engine(self, fake_engine_script):
        with _ForwarderHarness(fake_engine_script) as h:
            r = urllib.request.urlopen(f"{h.url}/v1/health", timeout=5)
            assert r.status == 200
            assert h.fw.manager._engine_alive()

    def test_get_forwarded(self, fake_engine_script):
        with _ForwarderHarness(fake_engine_script) as h:
            r = urllib.request.urlopen(f"{h.url}/echo/foo", timeout=5)
            data = json.loads(r.read())
            assert data["path"] == "/echo/foo"
            assert data["method"] == "GET"

    def test_post_with_body_forwarded(self, fake_engine_script):
        with _ForwarderHarness(fake_engine_script) as h:
            req = urllib.request.Request(
                f"{h.url}/v1/consult",
                data=b'{"message": "hi"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            r = urllib.request.urlopen(req, timeout=5)
            data = json.loads(r.read())
            assert data["method"] == "POST"
            assert "hi" in data["body"]

    def test_reaper_kills_idle_engine(self, fake_engine_script):
        with _ForwarderHarness(fake_engine_script,
                               idle=0.3, reap_interval=0.1) as h:
            urllib.request.urlopen(f"{h.url}/v1/health", timeout=5)
            assert h.fw.manager._engine_alive()
            # Wait past idle threshold + a couple reaper cycles.
            time.sleep(1.0)
            assert not h.fw.manager._engine_alive()

    def test_reaper_then_respawn(self, fake_engine_script):
        with _ForwarderHarness(fake_engine_script,
                               idle=0.3, reap_interval=0.1) as h:
            urllib.request.urlopen(f"{h.url}/v1/health", timeout=5)
            time.sleep(1.0)
            assert not h.fw.manager._engine_alive()
            # Next request respawns it.
            urllib.request.urlopen(f"{h.url}/v1/health", timeout=5)
            assert h.fw.manager._engine_alive()

    def test_503_when_engine_python_missing(self, fake_engine_script):
        cfg = cf.ForwarderConfig(
            listen_port=_free_port(),
            engine_python="/no/such/python",
        )
        fw = cf.Forwarder(cfg)
        try:
            fw.start()
            t = Thread(target=fw.httpd.serve_forever, daemon=True)
            t.start()
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{cfg.listen_port}/v1/health",
                    timeout=2,
                )
                assert False, "expected HTTPError"
            except urllib.error.HTTPError as e:
                assert e.code == 503
                body = json.loads(e.read())
                assert "engine_python not found" in body["detail"]
        finally:
            fw.shutdown()


# ----------------------- _resolve_engine_python ------------------ #

class TestResolveEnginePython:
    def test_pinned_env_var(self, tmp_path, monkeypatch):
        py = tmp_path / "fake_python"
        py.write_text("#!/bin/bash\nexit 0\n")
        py.chmod(0o755)
        monkeypatch.setenv("CLAUDE_CONSULTANTS_PY", str(py))
        assert cf._resolve_engine_python() == str(py)

    def test_returns_empty_when_nothing_found(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_CONSULTANTS_PY", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert cf._resolve_engine_python() == ""
