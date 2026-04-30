"""IPC plumbing for the LSP engine daemon.

UNIX-socket newline-delimited JSON, one request → one response. Each
request carries an ``id`` and a ``session`` string; the server
handler is a plain callable taking the request dict and returning a
response dict.

Phase 1 is POSIX-only — Windows named-pipe parity lands in Phase 4.
On non-POSIX platforms ``serve()`` raises ``NotImplementedError``.

Wire format::

    request:  {"id": 1, "session": "abc", "op": "did_open",
               "path": "/x.py", "content": "..."}\\n
    response: {"id": 1, "ok": true, "result": {...}}\\n
              {"id": 1, "ok": false, "error": "lock_held_by_other"}\\n

Newline-terminated JSON keeps the parser trivial (``readline`` +
``json.loads``) and lets us debug with ``socat -`` in a pinch.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import threading
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("claude_hooks.lsp_engine.ipc")


# Bound the per-request body size so a runaway client can't OOM the
# daemon. 16 MiB is generous (a 100 K-line Python file is ~3 MiB) and
# anything larger is almost certainly a buffer-attack bug.
MAX_REQUEST_BYTES = 16 * 1024 * 1024


RequestHandler = Callable[[dict], dict]


class IpcProtocolError(RuntimeError):
    """Wire-protocol violation (bad JSON, oversize frame, etc)."""


def _serve_connection(
    rfile,
    wfile,
    handler: RequestHandler,
    *,
    on_disconnect: Optional[Callable[[], None]] = None,
) -> None:
    """Generic one-conn-at-a-time dispatcher; works for both server
    and tests that wire two ends of a pipe directly.

    The connection ends when the peer closes (rfile.readline returns
    b'').  ``on_disconnect`` is called once at the end so the daemon
    can release any session locks tied to this connection.
    """
    try:
        while True:
            line = rfile.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_REQUEST_BYTES:
                raise IpcProtocolError("request exceeds MAX_REQUEST_BYTES")
            try:
                request = json.loads(line)
            except json.JSONDecodeError as e:
                raise IpcProtocolError(f"invalid JSON: {e}") from e
            if not isinstance(request, dict):
                raise IpcProtocolError("request must be a JSON object")
            try:
                response = handler(request)
            except Exception as e:  # pragma: no cover — defensive
                log.exception("ipc handler crashed")
                response = {
                    "id": request.get("id"),
                    "ok": False,
                    "error": f"handler crashed: {type(e).__name__}: {e}",
                }
            wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        # Client went away — normal during session shutdown.
        return
    finally:
        if on_disconnect is not None:
            try:
                on_disconnect()
            except Exception:  # pragma: no cover — defensive
                log.exception("on_disconnect callback raised")


class IpcServer:
    """Threaded UNIX-socket server that runs ``handler`` per request.

    Construct, call ``start()`` in the daemon's main thread, then
    ``serve_forever()`` (the daemon's main thread blocks here). Tests
    use ``start_in_background()`` instead and shut down via
    ``shutdown()``.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike,
        handler: RequestHandler,
        *,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        if os.name == "nt":
            raise NotImplementedError(
                "IpcServer requires UNIX sockets — Windows parity is Phase 4",
            )
        self._socket_path = Path(socket_path)
        self._handler = handler
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._server: Optional[socketserver.UnixStreamServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("IpcServer already started")
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Stale socket from a crashed daemon — it's safe to remove
        # *only* after we've verified no live process is listening.
        if self._socket_path.exists():
            if _is_socket_alive(self._socket_path):
                raise OSError(
                    f"socket {self._socket_path} is in use by another daemon",
                )
            self._socket_path.unlink()

        handler_cls = self._make_request_handler_cls()
        self._server = socketserver.ThreadingUnixStreamServer(
            str(self._socket_path), handler_cls
        )
        # Chmod 600 so other UIDs on a multi-user host can't poke the
        # daemon. Best-effort; failures are non-fatal.
        try:
            os.chmod(self._socket_path, 0o600)
        except OSError:  # pragma: no cover — defensive
            log.warning("could not chmod 600 %s", self._socket_path)

    def start_in_background(self) -> None:
        self.start()
        assert self._server is not None
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="lsp-engine-ipc",
            daemon=True,
        )
        self._thread.start()

    def serve_forever(self) -> None:
        if self._server is None:
            self.start()
        assert self._server is not None
        self._server.serve_forever()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self._socket_path.unlink()
        except FileNotFoundError:
            pass

    def _make_request_handler_cls(self) -> type[socketserver.StreamRequestHandler]:
        handler_fn = self._handler
        on_connect = self._on_connect
        on_disconnect = self._on_disconnect

        class _Handler(socketserver.StreamRequestHandler):
            def handle(self):  # type: ignore[override]
                if on_connect is not None:
                    try:
                        on_connect()
                    except Exception:  # pragma: no cover — defensive
                        log.exception("on_connect callback raised")
                _serve_connection(
                    self.rfile, self.wfile, handler_fn,
                    on_disconnect=on_disconnect,
                )

        return _Handler


def _is_socket_alive(socket_path: str | os.PathLike) -> bool:
    """Return True if *something* is listening on ``socket_path``.

    A stale UNIX socket from a crashed daemon will still appear in
    the filesystem, so we connect to disambiguate. Real listener →
    connect succeeds → return True. Nobody home → ``ConnectionRefused``
    or ``FileNotFoundError`` → False, safe to unlink.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect(str(socket_path))
        finally:
            sock.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


# ─── client side ─────────────────────────────────────────────────────


class IpcClient:
    """Synchronous client used by the hook to talk to the daemon.

    Holds one open socket; ``call()`` writes a request and reads the
    next response. NOT thread-safe — one IpcClient per caller. The
    daemon multiplexes multiple connections, so multiple Claude Code
    sessions just open multiple IpcClients.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike,
        *,
        timeout: float = 10.0,
    ) -> None:
        if os.name == "nt":
            raise NotImplementedError(
                "IpcClient requires UNIX sockets — Windows parity is Phase 4",
            )
        self._socket_path = Path(socket_path)
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._rfile = None
        self._wfile = None
        self._next_id = 1
        self._id_lock = threading.Lock()

    def connect(self) -> None:
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        sock.connect(str(self._socket_path))
        self._sock = sock
        self._rfile = sock.makefile("rb")
        self._wfile = sock.makefile("wb")

    def call(self, op: str, *, session: str, **params) -> dict:
        if self._sock is None:
            self.connect()
        assert self._rfile and self._wfile
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
        body = {"id": rid, "session": session, "op": op, **params}
        self._wfile.write((json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8"))
        self._wfile.flush()
        line = self._rfile.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            raise IpcProtocolError("daemon closed connection")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as e:
            raise IpcProtocolError(f"daemon returned invalid JSON: {e}") from e
        if not isinstance(response, dict):
            raise IpcProtocolError("daemon returned non-object response")
        if response.get("id") != rid:
            raise IpcProtocolError(
                f"id mismatch: sent {rid}, got {response.get('id')!r}",
            )
        return response

    def close(self) -> None:
        for f in (self._rfile, self._wfile):
            try:
                if f:
                    f.close()
            except Exception:  # pragma: no cover — defensive
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:  # pragma: no cover — defensive
                pass
        self._sock = None
        self._rfile = None
        self._wfile = None

    def __enter__(self) -> "IpcClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
