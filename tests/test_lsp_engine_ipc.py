"""Tests for the UNIX-socket IPC layer.

The IPC layer is the bridge between hook callers and the daemon. The
daemon's request handlers are tested separately (``test_lsp_engine_daemon``);
here we just verify the wire works: round-trips, multiple connections,
stale-socket cleanup, oversized payload rejection.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_hooks.lsp_engine.ipc import (
    MAX_REQUEST_BYTES,
    IpcClient,
    IpcServer,
    _is_socket_alive,
)


@unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
class TestIpcRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sock_path = Path(self.tmp.name) / "test.sock"
        self.disconnects = 0
        self._lock = threading.Lock()

        def echo(req: dict) -> dict:
            return {"id": req.get("id"), "ok": True, "echoed": req}

        def on_disc():
            with self._lock:
                self.disconnects += 1

        self.server = IpcServer(
            self.sock_path, handler=echo, on_disconnect=on_disc,
        )
        self.server.start_in_background()
        self.addCleanup(self.server.shutdown)

    def test_simple_request_response(self) -> None:
        with IpcClient(self.sock_path, timeout=2.0) as c:
            resp = c.call("ping", session="s1", payload="hello")
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["echoed"]["op"], "ping")
            self.assertEqual(resp["echoed"]["payload"], "hello")
            self.assertEqual(resp["echoed"]["session"], "s1")

    def test_id_increments_per_call(self) -> None:
        with IpcClient(self.sock_path, timeout=2.0) as c:
            r1 = c.call("op1", session="s1")
            r2 = c.call("op2", session="s1")
            self.assertEqual(r1["echoed"]["id"], 1)
            self.assertEqual(r2["echoed"]["id"], 2)

    def test_concurrent_connections_dont_interfere(self) -> None:
        results: dict[str, str] = {}
        errors: list[str] = []

        def worker(label: str) -> None:
            try:
                with IpcClient(self.sock_path, timeout=2.0) as c:
                    for i in range(5):
                        resp = c.call("ping", session=label, n=i)
                        # The id-mismatch check inside IpcClient.call
                        # would have raised if responses got mis-routed.
                        assert resp["echoed"]["session"] == label
                        results[f"{label}-{i}"] = resp["echoed"]["session"]
            except Exception as e:
                errors.append(f"{label}: {e}")

        threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8 * 5)

    def test_disconnect_callback_fires(self) -> None:
        c = IpcClient(self.sock_path, timeout=2.0)
        c.connect()
        c.call("ping", session="s1")
        c.close()
        # Server-side handler thread observes the disconnect on the
        # next read attempt. Poll briefly so the test isn't flaky.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with self._lock:
                if self.disconnects >= 1:
                    break
            time.sleep(0.020)
        self.assertGreaterEqual(self.disconnects, 1)


@unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
class TestStaleSocket(unittest.TestCase):
    def test_is_socket_alive_returns_false_for_missing_path(self) -> None:
        self.assertFalse(_is_socket_alive("/no/such/path.sock"))

    def test_stale_socket_file_gets_cleaned_on_start(self) -> None:
        with TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "stale.sock"
            # Create an orphan socket file (no listener) just by
            # binding-then-closing without listening.
            sock_path.touch()
            self.assertTrue(sock_path.exists())
            # Now start a fresh server — it should unlink the stale
            # file and bind cleanly.
            server = IpcServer(sock_path, handler=lambda req: {"id": req.get("id"), "ok": True})
            try:
                server.start_in_background()
                with IpcClient(sock_path, timeout=2.0) as c:
                    resp = c.call("ping", session="s1")
                    self.assertTrue(resp["ok"])
            finally:
                server.shutdown()

    def test_double_start_for_same_socket_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "race.sock"
            s1 = IpcServer(sock_path, handler=lambda req: {"id": req.get("id"), "ok": True})
            s1.start_in_background()
            try:
                s2 = IpcServer(sock_path, handler=lambda req: {"id": req.get("id"), "ok": True})
                with self.assertRaises(OSError):
                    s2.start_in_background()
            finally:
                s1.shutdown()


@unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
class TestProtocolErrors(unittest.TestCase):
    """The server should reply with structured errors on bad input
    rather than crashing the whole connection — but for *this* test
    we want to actually verify the parser's error path, so we send
    raw bytes."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sock_path = Path(self.tmp.name) / "err.sock"
        self.server = IpcServer(
            self.sock_path,
            handler=lambda req: {"id": req.get("id"), "ok": True},
        )
        self.server.start_in_background()
        self.addCleanup(self.server.shutdown)

    def _raw_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(str(self.sock_path))
        return s

    def test_invalid_json_kills_connection(self) -> None:
        s = self._raw_socket()
        try:
            s.sendall(b"{not json\n")
            # Server raises IpcProtocolError → connection closes.
            # readline returns b"" once the peer hangs up.
            data = s.recv(1024)
            self.assertEqual(data, b"")
        finally:
            s.close()

    def test_oversized_request_rejected(self) -> None:
        s = self._raw_socket()
        try:
            # Send a payload one byte beyond the cap, no trailing
            # newline yet — the readline call enforces the cap.
            payload = b"x" * (MAX_REQUEST_BYTES + 1) + b"\n"
            try:
                s.sendall(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
            # Connection should be closed by the server.
            try:
                data = s.recv(1024)
            except (ConnectionResetError, OSError):
                data = b""
            self.assertEqual(data, b"")
        finally:
            s.close()


@unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
class TestHandlerCrashesAreContained(unittest.TestCase):
    def test_handler_exception_returns_error_response_not_crash(self) -> None:
        with TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "boom.sock"

            def bad(req: dict) -> dict:
                raise RuntimeError("boom")

            server = IpcServer(sock_path, handler=bad)
            server.start_in_background()
            try:
                with IpcClient(sock_path, timeout=2.0) as c:
                    resp = c.call("ping", session="s1")
                    self.assertFalse(resp["ok"])
                    self.assertIn("boom", resp["error"])
                    # And the connection still works for a follow-up call.
                    resp2 = c.call("ping", session="s1")
                    self.assertFalse(resp2["ok"])
            finally:
                server.shutdown()


if __name__ == "__main__":
    unittest.main()
