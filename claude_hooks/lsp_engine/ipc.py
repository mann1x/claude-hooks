"""IPC plumbing for the LSP engine daemon — cross-platform.

POSIX uses UNIX-domain sockets via :mod:`socketserver` with
newline-delimited JSON: trivial framing (``readline`` + ``json.loads``)
that's debuggable with ``socat -``. Each request carries an ``id``
and a ``session`` string; the server handler is a plain callable
taking the request dict and returning a response dict.

Windows uses named pipes (``\\\\.\\pipe\\<name>``) via
:mod:`multiprocessing.connection`. The wire format on Windows is
auto-framed bytes (the connection handles message boundaries), so
each request is one ``send_bytes`` of a JSON-encoded payload and
each response is the matching ``recv_bytes``. No newlines.

Public API (``IpcServer`` / ``IpcClient``) is the same on both
platforms; the platform-specific impls are private and selected at
construction time. Tests on Linux exercise the POSIX backend
directly and the Windows backend via patched ``os.name``.

Wire format reference::

    POSIX (newline-delimited):
      request:  {"id": 1, "session": "abc", "op": "did_open",
                 "path": "/x.py", "content": "..."}\\n
      response: {"id": 1, "ok": true, "result": {...}}\\n

    Windows (auto-framed bytes):
      request:  send_bytes(json.dumps({...}).encode("utf-8"))
      response: recv_bytes() → bytes(json.dumps({...}).encode("utf-8"))
"""

from __future__ import annotations

import hashlib
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


# Windows pipe names live in the per-machine namespace ``\\.\pipe\``.
# Use a stable hash of the project root for the name suffix so two
# projects don't collide and we can derive the address without
# carrying a separate per-project file.
_WINDOWS_PIPE_PREFIX = r"\\.\pipe\claude-hooks-lsp-engine-"


class IpcProtocolError(RuntimeError):
    """Wire-protocol violation (bad JSON, oversize frame, etc)."""


# ─── public address helpers ──────────────────────────────────────────


def is_windows_address(address) -> bool:
    """Return True if ``address`` is a Windows named pipe.

    Accepts ``str``, ``Path``, or ``os.PathLike``. Used by callers
    that need to know which existence-check to apply (file stat for
    POSIX sockets, connection probe for Windows pipes).
    """
    # Pipe paths start with literal backslash-backslash-dot-backslash-pipe-backslash:
    # "\\.\pipe\<name>" — escaped here as "\\\\.\\pipe\\". Raw strings
    # can't end with a single backslash (Python syntax limitation), so
    # the prefix is spelled as a regular escaped string instead.
    return (
        str(address).startswith("\\\\.\\pipe\\")
        or str(address).startswith(_WINDOWS_PIPE_PREFIX)
    )


def windows_pipe_name_for(project_root: str | os.PathLike) -> str:
    """Build the named-pipe path Windows uses for ``project_root``.

    Hashed identically to the POSIX state directory so a daemon
    started under one address scheme can be located by tools that
    only know the other (rare but useful for diagnostics).
    """
    # Use os.path.abspath instead of pathlib here — pathlib's Path()
    # constructor inspects os.name at *call* time and tries to
    # instantiate WindowsPath when that's "nt". Tests patch os.name
    # to exercise the dispatch on a Linux host; pathlib then refuses
    # to construct WindowsPath. os.path.abspath has no such guard.
    abs_root = os.path.abspath(os.fspath(project_root))
    digest = hashlib.sha256(abs_root.encode("utf-8")).hexdigest()[:16]
    return _WINDOWS_PIPE_PREFIX + digest


# ─── shared helpers ──────────────────────────────────────────────────


def _serve_connection(
    rfile,
    wfile,
    handler: RequestHandler,
    *,
    on_disconnect: Optional[Callable[[], None]] = None,
) -> None:
    """POSIX dispatcher: read newline-framed JSON requests, hand
    them to ``handler``, write newline-framed responses."""
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
        return
    finally:
        if on_disconnect is not None:
            try:
                on_disconnect()
            except Exception:  # pragma: no cover — defensive
                log.exception("on_disconnect callback raised")


def _serve_pipe_connection(
    conn,
    handler: RequestHandler,
    *,
    on_disconnect: Optional[Callable[[], None]] = None,
) -> None:
    """Windows dispatcher: drive a ``multiprocessing.connection``
    pipe with auto-framed bytes (one request, one response).
    """
    try:
        while True:
            try:
                data = conn.recv_bytes(maxlength=MAX_REQUEST_BYTES + 1)
            except (EOFError, ConnectionResetError, OSError):
                break
            if not data:
                break
            if len(data) > MAX_REQUEST_BYTES:  # pragma: no cover — protective
                break
            try:
                request = json.loads(data)
            except json.JSONDecodeError:
                # Drop the connection on garbage — same as POSIX path.
                break
            if not isinstance(request, dict):
                break
            try:
                response = handler(request)
            except Exception as e:  # pragma: no cover — defensive
                log.exception("ipc handler crashed")
                response = {
                    "id": request.get("id"),
                    "ok": False,
                    "error": f"handler crashed: {type(e).__name__}: {e}",
                }
            try:
                conn.send_bytes(json.dumps(response, ensure_ascii=False).encode("utf-8"))
            except (BrokenPipeError, OSError):
                break
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover — defensive
            pass
        if on_disconnect is not None:
            try:
                on_disconnect()
            except Exception:  # pragma: no cover — defensive
                log.exception("on_disconnect callback raised")


# ─── server ──────────────────────────────────────────────────────────


class IpcServer:
    """Threaded server. Auto-selects the POSIX UNIX-socket backend
    or the Windows named-pipe backend at construction time.

    On POSIX, ``socket_path`` is the filesystem path of the socket.
    On Windows, it can be either a ``\\\\.\\pipe\\<name>`` literal or
    a value derived from :func:`windows_pipe_name_for` —
    :func:`is_windows_address` decides.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike,
        handler: RequestHandler,
        *,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        self._socket_path = socket_path
        self._handler = handler
        if os.name == "nt" or is_windows_address(socket_path):
            self._impl: _IpcServerImpl = _WindowsPipeServer(
                socket_path, handler,
                on_connect=on_connect, on_disconnect=on_disconnect,
            )
        else:
            self._impl = _UnixSocketServer(
                socket_path, handler,
                on_connect=on_connect, on_disconnect=on_disconnect,
            )

    @property
    def socket_path(self):
        return self._impl.socket_path

    def start(self) -> None:
        self._impl.start()

    def start_in_background(self) -> None:
        self._impl.start_in_background()

    def serve_forever(self) -> None:
        self._impl.serve_forever()

    def shutdown(self) -> None:
        self._impl.shutdown()


class _IpcServerImpl:
    """Common interface the platform-specific impls satisfy."""

    socket_path: object

    def start(self) -> None: ...
    def start_in_background(self) -> None: ...
    def serve_forever(self) -> None: ...
    def shutdown(self) -> None: ...


class _UnixSocketServer(_IpcServerImpl):
    """POSIX socketserver-based backend (Phase 1 implementation)."""

    def __init__(
        self,
        socket_path: str | os.PathLike,
        handler: RequestHandler,
        *,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        self._socket_path: Path = Path(socket_path)
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
        if self._socket_path.exists():
            if _is_unix_socket_alive(self._socket_path):
                raise OSError(
                    f"socket {self._socket_path} is in use by another daemon",
                )
            self._socket_path.unlink()

        handler_cls = self._make_request_handler_cls()
        self._server = socketserver.ThreadingUnixStreamServer(
            str(self._socket_path), handler_cls
        )
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


class _WindowsPipeServer(_IpcServerImpl):
    """Windows named-pipe backend.

    Built on :mod:`multiprocessing.connection` so we get the right
    NamedPipe primitives without an extra dep (pywin32). Each
    accepted connection runs in its own thread; shutdown closes the
    listener which makes the accept loop bail out.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike,
        handler: RequestHandler,
        *,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        self._pipe_name = str(socket_path)
        self._handler = handler
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._listener = None  # multiprocessing.connection.Listener
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._conn_threads: list[threading.Thread] = []

    @property
    def socket_path(self) -> str:
        return self._pipe_name

    def start(self) -> None:
        if self._listener is not None:
            raise RuntimeError("IpcServer already started")
        # Imported here so the module loads on POSIX without paying
        # the multiprocessing.connection cost up front.
        from multiprocessing.connection import Listener
        self._listener = Listener(self._pipe_name, family="AF_PIPE")

    def start_in_background(self) -> None:
        self.start()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="lsp-engine-ipc",
            daemon=True,
        )
        self._accept_thread.start()

    def serve_forever(self) -> None:
        if self._listener is None:
            self.start()
        self._accept_loop()

    def shutdown(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except Exception:  # pragma: no cover — defensive
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
        # Best-effort drain of running connection threads. They're
        # daemon=True so they don't block process exit either way.
        for t in list(self._conn_threads):
            t.join(timeout=0.5)
        self._conn_threads.clear()

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                conn = listener.accept()
            except (OSError, EOFError):
                # Listener.close() during accept raises here.
                return
            except Exception:  # pragma: no cover — defensive
                log.exception("accept loop unexpected error")
                return
            if self._on_connect is not None:
                try:
                    self._on_connect()
                except Exception:  # pragma: no cover — defensive
                    log.exception("on_connect callback raised")
            t = threading.Thread(
                target=_serve_pipe_connection,
                args=(conn, self._handler),
                kwargs={"on_disconnect": self._on_disconnect},
                name="lsp-engine-pipe-conn",
                daemon=True,
            )
            t.start()
            self._conn_threads.append(t)


# ─── existence probe ─────────────────────────────────────────────────


def _is_socket_alive(socket_path: str | os.PathLike) -> bool:
    """Cross-platform "is the daemon listening at this address?"
    check. Returns True iff *something* is accepting connections.
    """
    if is_windows_address(socket_path) or os.name == "nt":
        return _is_pipe_alive(str(socket_path))
    return _is_unix_socket_alive(socket_path)


def _is_unix_socket_alive(socket_path: str | os.PathLike) -> bool:
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


def _is_pipe_alive(pipe_name: str) -> bool:
    """Probe a Windows named pipe: open it, close immediately."""
    try:
        from multiprocessing.connection import Client
        conn = Client(pipe_name, family="AF_PIPE")
        conn.close()
        return True
    except (FileNotFoundError, OSError):
        return False
    except Exception:  # pragma: no cover — defensive
        return False


# ─── client ──────────────────────────────────────────────────────────


class IpcClient:
    """Synchronous client. Auto-selects platform backend.

    Holds one open connection; ``call()`` writes a request and reads
    the next response. NOT thread-safe — one IpcClient per caller.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._socket_path = socket_path
        self._timeout = timeout
        if os.name == "nt" or is_windows_address(socket_path):
            self._impl: _IpcClientImpl = _WindowsPipeClient(
                socket_path, timeout=timeout,
            )
        else:
            self._impl = _UnixSocketClient(socket_path, timeout=timeout)

    def connect(self) -> None:
        self._impl.connect()

    def call(self, op: str, *, session: str, **params) -> dict:
        return self._impl.call(op, session=session, **params)

    def close(self) -> None:
        self._impl.close()

    def __enter__(self) -> "IpcClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _IpcClientImpl:
    def connect(self) -> None: ...
    def call(self, op: str, *, session: str, **params) -> dict: ...
    def close(self) -> None: ...


class _UnixSocketClient(_IpcClientImpl):
    """Phase 1 client retained verbatim under the new dispatch."""

    def __init__(
        self,
        socket_path: str | os.PathLike,
        *,
        timeout: float = 10.0,
    ) -> None:
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


class _WindowsPipeClient(_IpcClientImpl):
    """Windows client driving a ``multiprocessing.connection`` pipe."""

    def __init__(
        self,
        socket_path: str | os.PathLike,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._pipe_name = str(socket_path)
        self._timeout = timeout
        self._conn = None  # multiprocessing.connection.Connection
        self._next_id = 1
        self._id_lock = threading.Lock()

    def connect(self) -> None:
        if self._conn is not None:
            return
        from multiprocessing.connection import Client
        self._conn = Client(self._pipe_name, family="AF_PIPE")

    def call(self, op: str, *, session: str, **params) -> dict:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
        body = {"id": rid, "session": session, "op": op, **params}
        try:
            self._conn.send_bytes(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, OSError) as e:
            raise IpcProtocolError(f"failed to send: {e}") from e
        try:
            data = self._conn.recv_bytes(maxlength=MAX_REQUEST_BYTES + 1)
        except (EOFError, ConnectionResetError, OSError) as e:
            raise IpcProtocolError(f"daemon closed connection: {e}") from e
        try:
            response = json.loads(data)
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
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover — defensive
                pass
            self._conn = None
