"""Tests for the claude-hooks-daemon (Tier 3.8 latency reduction)."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from claude_hooks import daemon, daemon_client


# ===================================================================== #
# Secret management
# ===================================================================== #
class TestEnsureSecret:
    def test_creates_secret_when_missing(self, tmp_path):
        path = tmp_path / "secret"
        s = daemon.ensure_secret(path)
        assert isinstance(s, str)
        assert len(s) >= 32  # token_hex(32) = 64 hex chars
        assert path.exists()

    def test_reads_existing_secret_unchanged(self, tmp_path):
        path = tmp_path / "secret"
        path.write_text("preset-secret-value", encoding="utf-8")
        if os.name == "posix":
            os.chmod(path, 0o600)
        s = daemon.ensure_secret(path)
        assert s == "preset-secret-value"

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission check")
    def test_refuses_world_readable_secret(self, tmp_path):
        path = tmp_path / "secret"
        path.write_text("leaky", encoding="utf-8")
        os.chmod(path, 0o644)
        with pytest.raises(RuntimeError, match="world/group-readable"):
            daemon.ensure_secret(path)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission check")
    def test_new_secret_has_strict_perms(self, tmp_path):
        path = tmp_path / "secret"
        daemon.ensure_secret(path)
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600


# ===================================================================== #
# HMAC sign / verify
# ===================================================================== #
class TestSignVerify:
    def test_round_trip(self):
        sig = daemon.sign_request(
            request_id=1, ts=1000.0, event="Stop",
            payload_json='{"a":1}', secret="s",
        )
        err = daemon.verify_request(
            request_id=1, ts=1000.0, event="Stop",
            payload_json='{"a":1}', signature=sig, secret="s",
            now=1000.0,
        )
        assert err is None

    def test_verify_rejects_bad_signature(self):
        err = daemon.verify_request(
            request_id=1, ts=1000.0, event="Stop",
            payload_json='{"a":1}', signature="nope",
            secret="s", now=1000.0,
        )
        assert err == "invalid signature"

    def test_verify_rejects_replay_outside_window(self):
        sig = daemon.sign_request(
            request_id=1, ts=1000.0, event="Stop",
            payload_json='{}', secret="s",
        )
        # 120 s drift exceeds the default 60 s window.
        err = daemon.verify_request(
            request_id=1, ts=1000.0, event="Stop",
            payload_json='{}', signature=sig, secret="s",
            now=1120.0,
        )
        assert err is not None and "replay" in err

    def test_signature_changes_with_payload(self):
        a = daemon.sign_request(
            request_id=1, ts=1000.0, event="Stop",
            payload_json='{"a":1}', secret="s",
        )
        b = daemon.sign_request(
            request_id=1, ts=1000.0, event="Stop",
            payload_json='{"a":2}', secret="s",
        )
        assert a != b

    def test_signature_changes_with_secret(self):
        a = daemon.sign_request(
            request_id=1, ts=1000.0, event="Stop",
            payload_json='{}', secret="secret-a",
        )
        b = daemon.sign_request(
            request_id=1, ts=1000.0, event="Stop",
            payload_json='{}', secret="secret-b",
        )
        assert a != b


# ===================================================================== #
# Server bind safety
# ===================================================================== #
class TestServerBindSafety:
    def test_refuses_external_address(self):
        with pytest.raises(ValueError, match="non-loopback"):
            daemon.DaemonServer("0.0.0.0", 0, secret="s")

    def test_refuses_arbitrary_address(self):
        with pytest.raises(ValueError):
            daemon.DaemonServer("192.168.1.5", 0, secret="s")

    def test_accepts_loopback(self):
        # port 0 = pick any free port — we close immediately.
        srv = daemon.DaemonServer("127.0.0.1", 0, secret="s")
        try:
            assert srv.server_address[0] == "127.0.0.1"
            assert srv.server_address[1] > 0
        finally:
            srv.server_close()


# ===================================================================== #
# End-to-end: real daemon + real client
# ===================================================================== #
@pytest.fixture
def running_daemon(tmp_path):
    """Spin up a daemon on an ephemeral port, yield its (host, port, secret_path)."""
    secret_path = tmp_path / "secret"
    secret = daemon.ensure_secret(secret_path)
    srv = daemon.DaemonServer("127.0.0.1", 0, secret=secret)
    host, port = srv.server_address
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        yield host, port, secret_path
    finally:
        srv.shutdown()
        srv.server_close()


class TestClientServerRoundTrip:
    def test_ping_returns_true_against_running_daemon(self, running_daemon):
        host, port, secret_path = running_daemon
        assert daemon_client.ping(
            host=host, port=port, secret_path=secret_path,
        ) is True

    def test_ping_returns_false_against_no_daemon(self, tmp_path):
        # Pick a port that is almost certainly closed.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        # Now nothing listens on free_port.
        assert daemon_client.ping(
            host="127.0.0.1", port=free_port,
            secret_path=tmp_path / "missing-secret",
        ) is False

    def test_call_returns_none_when_secret_missing(self, tmp_path):
        # Even if a daemon is up, no secret = client cannot sign — fallback.
        resp = daemon_client.call(
            "_ping", {},
            host="127.0.0.1", port=12345,  # unused — secret check first
            secret_path=tmp_path / "no-such-file",
        )
        assert resp is None

    def test_call_returns_none_when_daemon_down(self, tmp_path):
        secret_path = tmp_path / "secret"
        daemon.ensure_secret(secret_path)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        resp = daemon_client.call(
            "_ping", {},
            host="127.0.0.1", port=free_port,
            secret_path=secret_path, timeout=0.5,
        )
        assert resp is None

    def test_call_with_bad_secret_returns_auth_error(
        self, running_daemon, tmp_path,
    ):
        host, port, _ = running_daemon
        # Use a different secret path than the running daemon.
        wrong_secret_path = tmp_path / "wrong-secret"
        wrong_secret_path.write_text("totally-different-secret", encoding="utf-8")
        if os.name == "posix":
            os.chmod(wrong_secret_path, 0o600)
        resp = daemon_client.call(
            "_ping", {},
            host=host, port=port, secret_path=wrong_secret_path,
        )
        assert resp is not None
        assert resp.get("ok") is False
        assert resp.get("code") == 401

    def test_dispatched_event_returns_handler_output(self, running_daemon, monkeypatch):
        """Daemon should run the dispatcher and return its JSON output.

        The daemon calls :func:`dispatcher.dispatch_capture` (which
        returns a dict directly) rather than the legacy stdout-writing
        :func:`dispatcher.dispatch` — that's what makes concurrent
        dispatches thread-safe (see TestDispatchCaptureThreadSafety in
        test_dispatcher.py for the regression coverage).
        """
        host, port, secret_path = running_daemon

        from claude_hooks import dispatcher

        def fake_dispatch_capture(event_name, event):
            return {"hook": event_name, "got": event}

        monkeypatch.setattr(dispatcher, "dispatch_capture", fake_dispatch_capture)

        resp = daemon_client.call(
            "Stop", {"session_id": "abc"},
            host=host, port=port, secret_path=secret_path,
        )
        assert resp is not None
        assert resp.get("ok") is True
        assert resp["result"] == {"hook": "Stop", "got": {"session_id": "abc"}}

    def test_handler_with_no_output_returns_null_result(
        self, running_daemon, monkeypatch,
    ):
        host, port, secret_path = running_daemon
        from claude_hooks import dispatcher
        monkeypatch.setattr(
            dispatcher, "dispatch_capture", lambda event_name, event: None,
        )
        resp = daemon_client.call(
            "SessionEnd", {},
            host=host, port=port, secret_path=secret_path,
        )
        assert resp is not None and resp.get("ok") is True
        assert resp["result"] is None


class TestShutdownClient:
    """``daemon_client.shutdown`` is the graceful stop verb used by the
    daemon-ctl wrapper. Confirms the request is signed, the response
    arrives before the server tears down, and the daemon does in fact
    stop accepting new connections shortly afterwards."""

    def test_shutdown_returns_true_against_running_daemon(self, running_daemon):
        host, port, secret_path = running_daemon
        assert daemon_client.shutdown(
            host=host, port=port, secret_path=secret_path,
        ) is True

    def test_shutdown_actually_stops_the_daemon(self, running_daemon):
        host, port, secret_path = running_daemon
        # Daemon is up — confirm before asking it to stop.
        assert daemon_client.ping(
            host=host, port=port, secret_path=secret_path,
        ) is True

        assert daemon_client.shutdown(
            host=host, port=port, secret_path=secret_path,
        ) is True

        # Give the side thread a moment to actually close the socket.
        # ``request_shutdown`` schedules ``server.shutdown()`` on a
        # background thread so the response can flush first.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if daemon_client.ping(
                host=host, port=port, secret_path=secret_path,
                timeout=0.5,
            ) is False:
                return
            time.sleep(0.05)
        pytest.fail("daemon still responding 3 s after shutdown ack")

    def test_shutdown_returns_false_when_daemon_down(self, tmp_path):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        assert daemon_client.shutdown(
            host="127.0.0.1", port=free_port,
            secret_path=tmp_path / "missing-secret",
        ) is False

    def test_shutdown_returns_false_on_auth_failure(
        self, running_daemon, tmp_path,
    ):
        host, port, _ = running_daemon
        wrong = tmp_path / "wrong-secret"
        wrong.write_text("nope-not-the-real-secret", encoding="utf-8")
        if os.name == "posix":
            os.chmod(wrong, 0o600)
        # Bad secret → daemon replies 401, our helper treats as failure.
        # Daemon must remain up afterwards so the test doesn't break the
        # fixture cleanup.
        assert daemon_client.shutdown(
            host=host, port=port, secret_path=wrong,
        ) is False
        # Sanity: real ping with the real secret (loaded by the fixture)
        # is not part of this helper's contract — but the daemon is
        # definitely not torn down. Best we can verify here is that the
        # fixture's teardown completes without hanging.


# ===================================================================== #
# Bad-request handling — server rejects without crashing
# ===================================================================== #
def _send_raw_line(host: str, port: int, line: bytes, timeout: float = 2.0) -> bytes:
    """Send a raw line to the daemon and return the raw response bytes."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(line)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        chunks: list[bytes] = []
        while True:
            try:
                buf = sock.recv(4096)
            except (socket.timeout, TimeoutError):
                break
            if not buf:
                break
            chunks.append(buf)
        return b"".join(chunks)


class TestBadRequests:
    def test_invalid_json_yields_400(self, running_daemon):
        host, port, _ = running_daemon
        raw = _send_raw_line(host, port, b"not json {{\n")
        resp = json.loads(raw.decode("utf-8").splitlines()[0])
        assert resp.get("ok") is False
        assert resp.get("code") == 400

    def test_missing_id_yields_400(self, running_daemon):
        host, port, _ = running_daemon
        raw = _send_raw_line(host, port, b'{"event":"x","ts":1,"sig":"x"}\n')
        resp = json.loads(raw.decode("utf-8").splitlines()[0])
        assert resp.get("ok") is False
        assert resp.get("code") == 400

    def test_server_survives_bad_request(self, running_daemon):
        """A bad request must not kill the server loop."""
        host, port, secret_path = running_daemon
        # Send a bad request, then a good one — second must succeed.
        _send_raw_line(host, port, b"garbage\n")
        assert daemon_client.ping(
            host=host, port=port, secret_path=secret_path,
        ) is True
