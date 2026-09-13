"""Per-language LSP child wrapper.

Speaks the standard LSP wire protocol over the child's stdin/stdout:
``Content-Length: N\\r\\n\\r\\n`` followed by N bytes of UTF-8 JSON-RPC
2.0. Runs a background reader thread so async notifications from the
server (``textDocument/publishDiagnostics``) don't deadlock callers
waiting on a synchronous request response.

The contract is intentionally narrow for Phase 0:

- ``start()`` spawns the LSP and runs the ``initialize`` handshake.
- ``did_open(uri, language_id, content)`` opens a buffer in the LSP.
- ``did_change(uri, content)`` replaces the buffer (full document
  sync — incremental sync is a Phase 2 optimisation).
- ``get_diagnostics(uri, timeout)`` blocks until the server has
  published diagnostics for ``uri`` since the last call, or the
  timeout expires; returns the latest list.
- ``stop()`` sends ``shutdown`` + ``exit`` and reaps the child.

No goto-def / hover / references yet — those land once the IPC layer
is in place in Phase 1 and we have a real consumer for them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Optional
from urllib.parse import unquote

log = logging.getLogger("claude_hooks.lsp_engine.lsp")


_LANGUAGE_ID_BY_EXT = {
    "py": "python",
    "pyi": "python",
    "go": "go",
    "rs": "rust",
    "c": "c",
    "cc": "cpp",
    "cpp": "cpp",
    "cxx": "cpp",
    "h": "c",
    "hh": "cpp",
    "hpp": "cpp",
    "hxx": "cpp",
    "cs": "csharp",
    "ts": "typescript",
    "tsx": "typescriptreact",
    "js": "javascript",
    "jsx": "javascriptreact",
    # ESM/CJS TypeScript. typescript-language-server claims these in
    # the installer matrix, and a document announced as "plaintext" is
    # one most servers decline to analyse — so an extension mapped in
    # cclsp.json but missing here produces a *silent* no-diagnostics,
    # which is the same shape as the two bugs above it.
    "mts": "typescript",
    "cts": "typescript",
    "sh": "shellscript",
    "bash": "shellscript",
    "lua": "lua",
    "zig": "zig",
}


def language_id_for(path: str | os.PathLike) -> str:
    """Map a filesystem path to its LSP ``languageId``.

    Returns ``"plaintext"`` for unknown extensions — most LSPs reject
    documents they don't claim, but a stray file shouldn't crash the
    wrapper before the LSP gets a chance to refuse it.
    """
    ext = Path(path).suffix.lower().lstrip(".")
    return _LANGUAGE_ID_BY_EXT.get(ext, "plaintext")


def path_to_uri(path: str | os.PathLike) -> str:
    """Convert an absolute filesystem path to a ``file://`` URI.

    LSP servers reject relative paths and Windows-style backslashes,
    so always normalise via ``Path.as_uri()`` after resolving.
    """
    return Path(path).resolve().as_uri()


_FILE_DRIVE_RE = re.compile(r"^(file:///)([a-zA-Z])(?::|%3[Aa]|\|)(/.*)?$")


def uri_key(uri: str) -> str:
    """Canonical dictionary key for a ``file://`` URI.

    Windows LSP servers do not spell a file URI the way Python does.
    ``Path.as_uri()`` produces ``file:///C:/x/a.py``; pyright — and
    anything else built on ``vscode-uri``, which is most of them —
    publishes ``file:///c%3A/x/a.py``: lowercased drive letter, colon
    percent-encoded. Same file, two strings.

    That mismatch silently destroyed every diagnostic on Windows. The
    server answered in under a second, the engine filed the result
    under the key the *server* used, and every lookup asked for the key
    *we* built — so `diagnostics()` waited out its timeout and returned
    an empty list, which reads exactly like a clean file. It survived
    because POSIX has no drive letter to disagree about: there the two
    spellings are identical and this function is the identity.

    Only the dictionary key is normalised. The URI sent on the wire
    stays whatever ``path_to_uri`` produced, because that is a
    well-formed URI the servers accept — the disagreement is about
    which of two valid spellings comes back.
    """
    s = unquote(uri).replace("\\", "/")
    m = _FILE_DRIVE_RE.match(s)
    if m:
        return f"{m.group(1)}{m.group(2).upper()}:{m.group(3) or '/'}"
    return s


def _resolve_binary(name: str) -> str:
    """Return an executable path for ``name``, or ``name`` unchanged.

    ``Popen`` cannot be trusted to find the server on Windows. It calls
    ``CreateProcess``, which appends ``.exe`` and consults nothing else
    — in particular not ``PATHEXT``. npm installs its global binaries
    as ``.CMD`` shims, so ``pyright-langserver``,
    ``typescript-language-server`` and ``bash-language-server`` are
    *invisible* to it while sitting on ``PATH`` in plain view.
    ``shutil.which`` does apply ``PATHEXT``, and a resolved ``.CMD``
    launches fine (``CreateProcess`` routes batch files through
    ``cmd.exe``; ``stop()`` shuts the server down over LSP's own
    ``shutdown``/``exit`` on the pipes, which reaches it through that
    intermediate).

    The symptom this fixes is not a crash. ``PostToolUse`` catches the
    spawn failure, logs a warning and returns no block, so on Windows
    every TypeScript, Python and bash edit simply produced no
    diagnostics — while Go, Rust and C++ worked, because those ship
    real ``.exe``s and ``CreateProcess``'s one hardcoded extension is
    enough for them.

    Resolving on POSIX too is deliberate: ``which`` performs the same
    ``PATH`` search ``Popen`` would, so behaviour is unchanged, and one
    code path beats a platform branch that only the minority platform
    ever exercises. An absolute or relative path is returned untouched
    — the caller meant that exact file. When nothing resolves, the name
    comes back unchanged so the caller's "not found" error can name
    what the user configured.
    """
    if os.path.isabs(name):
        return name
    if os.sep in name or (os.altsep and os.altsep in name):
        return name
    return shutil.which(name) or name


@dataclass
class Diagnostic:
    """Subset of LSP ``Diagnostic`` we surface to callers."""

    uri: str
    severity: int  # 1=error, 2=warning, 3=info, 4=hint
    line: int
    character: int
    message: str
    code: Optional[str] = None
    source: Optional[str] = None


class LspError(RuntimeError):
    """Raised when the LSP returns an error response or fails to start."""


class LspProtocolError(LspError):
    """Raised when the wire protocol is violated (bad framing, etc)."""


class LspClient:
    """One LSP child process, owned by exactly one caller.

    Not thread-safe for concurrent ``did_change`` / ``did_open``
    callers — the daemon (Phase 1) serialises through the IPC layer.
    The reader thread is the only thread that touches stdout.
    """

    def __init__(
        self,
        command: list[str] | tuple[str, ...],
        root_dir: str | os.PathLike,
        *,
        startup_timeout: float = 10.0,
        request_timeout: float = 5.0,
    ) -> None:
        self._command = list(command)
        self._root_dir = Path(root_dir).resolve()
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout

        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()

        self._next_id = 1
        self._id_lock = threading.Lock()

        # Pending request-id -> Event/Queue pair so the reader thread
        # can wake the caller waiting on the response.
        self._pending: dict[int, Queue] = {}
        self._pending_lock = threading.Lock()

        # Latest diagnostics per URI. Replaced (not appended) on each
        # ``publishDiagnostics`` notification — that matches LSP
        # semantics: the server always sends the *current* full set.
        self._diagnostics: dict[str, list[Diagnostic]] = {}
        self._diagnostics_event: dict[str, threading.Event] = {}
        self._diag_lock = threading.Lock()

        self._open_versions: dict[str, int] = {}
        # Drop publishDiagnostics whose ``version`` is older than the
        # last did_change we sent. Without this guard, a delayed
        # publish for didOpen v1 can land *after* we reset for
        # didChange v2 and pollute state with stale len-5 diagnostics
        # the test thread then reads.
        self._diag_min_version: dict[str, int] = {}

    # ─── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        if self._proc is not None:
            raise LspError("LspClient already started")

        # Windows-only: the LSP engine daemon spawns this LspClient
        # process to talk to pyright / gopls / clangd / etc. The
        # daemon itself is windowless (it is spawned with
        # ``CREATE_NO_WINDOW | DETACHED_PROCESS`` by
        # :func:`claude_hooks.lsp_engine.client.connect_or_spawn`), so
        # when *it* spawns one of these LSP children with no
        # ``creationflags``, Windows allocates a brand-new console
        # window for the child by default — i.e. every pyright /
        # gopls / clangd spawn pops a console on the user's desktop.
        # We deliberately do NOT add ``DETACHED_PROCESS`` here
        # because we need the stdin / stdout / stderr pipes for LSP
        # JSON-RPC (``DETACHED_PROCESS`` severs the inherited stdio
        # handles, breaking the protocol). ``CREATE_NO_WINDOW`` alone
        # is the minimal fix — keep pipes, suppress the console.
        # Mirrors the inline pattern in
        # :meth:`EmbeddingManager._spawn_once` / equivalent, but
        # without the detach pair.
        popen_kwargs: dict = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": str(self._root_dir),
            "bufsize": 0,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NO_WINDOW", 0,
            )

        # Resolve the binary ourselves rather than letting Popen search
        # PATH — see :func:`_resolve_binary`. The error below still
        # names the *configured* command, not the resolution, so a
        # genuinely missing server reads the way the user wrote it.
        cmd = list(self._command)
        cmd[0] = _resolve_binary(cmd[0])

        try:
            self._proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError as e:
            raise LspError(f"LSP binary not found: {self._command[0]}") from e

        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name=f"lsp-reader-{self._command[0]}",
            daemon=True,
        )
        self._reader_thread.start()

        # Initialize handshake — required before any other request.
        # The deadline applies to the whole handshake, not each step.
        deadline = time.monotonic() + self._startup_timeout
        result = self._send_request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self._root_dir.as_uri(),
                "capabilities": {
                    "textDocument": {
                        "synchronization": {
                            "didSave": False,
                            "willSave": False,
                        },
                        "publishDiagnostics": {
                            "relatedInformation": False,
                        },
                    },
                },
                "clientInfo": {"name": "claude-hooks-lsp-engine", "version": "0.1.0"},
            },
            timeout=max(0.1, deadline - time.monotonic()),
        )
        if not isinstance(result, dict) or "capabilities" not in result:
            raise LspProtocolError(
                f"initialize returned unexpected payload: {result!r}",
            )
        self._send_notification("initialized", {})

    def stop(self, *, timeout: float = 3.0) -> None:
        if self._proc is None:
            return
        try:
            try:
                self._send_request("shutdown", None, timeout=timeout)
                self._send_notification("exit", None)
            except (LspError, BrokenPipeError, OSError):
                # Server already gone — fall through to terminate.
                pass
            self._stop_requested.set()
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)
        finally:
            self._proc = None
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=1.0)
                self._reader_thread = None

    def __enter__(self) -> "LspClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ─── document operations ─────────────────────────────────────────

    def did_open(self, path: str | os.PathLike, content: str) -> None:
        uri = path_to_uri(path)
        key = uri_key(uri)
        version = self._open_versions.get(key, 0) + 1
        self._open_versions[key] = version
        self._reset_diagnostics(key, expected_version=version)
        self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id_for(path),
                    "version": version,
                    "text": content,
                },
            },
        )

    def did_change(self, path: str | os.PathLike, content: str) -> None:
        uri = path_to_uri(path)
        key = uri_key(uri)
        if key not in self._open_versions:
            raise LspError(
                f"did_change before did_open: {uri}",
            )
        self._open_versions[key] += 1
        version = self._open_versions[key]
        self._reset_diagnostics(key, expected_version=version)
        self._send_notification(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": content}],
            },
        )

    def did_close(self, path: str | os.PathLike) -> None:
        uri = path_to_uri(path)
        self._open_versions.pop(uri_key(uri), None)
        self._send_notification(
            "textDocument/didClose",
            {"textDocument": {"uri": uri}},
        )

    # ─── diagnostics ─────────────────────────────────────────────────

    def get_diagnostics(
        self,
        path: str | os.PathLike,
        *,
        timeout: float = 2.0,
    ) -> list[Diagnostic]:
        """Block until the server publishes diagnostics for ``path``,
        or ``timeout`` elapses; return the latest list.

        If the server has already published since the last reset (i.e.
        since the last ``did_open`` / ``did_change`` for this URI),
        return immediately. Returns an empty list on timeout — callers
        should distinguish "no diagnostics yet" from "no diagnostics"
        via the timeout themselves if they care.
        """
        key = uri_key(path_to_uri(path))
        with self._diag_lock:
            event = self._diagnostics_event.setdefault(key, threading.Event())
            if event.is_set():
                return list(self._diagnostics.get(key, []))
        if not event.wait(timeout=timeout):
            return []
        with self._diag_lock:
            return list(self._diagnostics.get(key, []))

    def _reset_diagnostics(self, key: str, *, expected_version: int) -> None:
        """``key`` is a :func:`uri_key` result, never a raw URI — the
        two differ on Windows and mixing them is the bug this rename
        exists to prevent."""
        with self._diag_lock:
            self._diagnostics.pop(key, None)
            self._diag_min_version[key] = expected_version
            event = self._diagnostics_event.get(key)
            if event is not None:
                event.clear()

    # ─── wire protocol ───────────────────────────────────────────────

    def _next_request_id(self) -> int:
        with self._id_lock:
            i = self._next_id
            self._next_id += 1
            return i

    def _send_request(
        self,
        method: str,
        params,
        *,
        timeout: Optional[float] = None,
    ):
        if self._proc is None or self._proc.stdin is None:
            raise LspError("LSP not started")
        rid = self._next_request_id()
        q: Queue = Queue(maxsize=1)
        with self._pending_lock:
            self._pending[rid] = q
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write_frame(msg)
        try:
            response = q.get(timeout=timeout if timeout is not None else self._request_timeout)
        except Empty:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise LspError(f"timeout waiting for response to {method!r}")
        if "error" in response:
            err = response["error"]
            raise LspError(f"{method} -> error {err.get('code')}: {err.get('message')}")
        return response.get("result")

    def _send_notification(self, method: str, params) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise LspError("LSP not started")
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write_frame(msg)

    def _write_frame(self, msg: dict) -> None:
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        assert self._proc and self._proc.stdin
        try:
            self._proc.stdin.write(header + body)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise LspError(f"failed to write LSP frame: {e}") from e

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        stdout = self._proc.stdout
        try:
            while not self._stop_requested.is_set():
                msg = self._read_frame(stdout)
                if msg is None:
                    return  # EOF
                self._dispatch(msg)
        except Exception:  # pragma: no cover — defensive
            log.exception("lsp reader thread crashed")

    @staticmethod
    def _read_frame(stream) -> Optional[dict]:
        # Read headers until blank line.
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if not line:
                return None
            line = line.decode("ascii", errors="replace").rstrip("\r\n")
            if line == "":
                break
            if ":" not in line:
                raise LspProtocolError(f"malformed header line: {line!r}")
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        length_str = headers.get("content-length")
        if not length_str:
            raise LspProtocolError("missing Content-Length header")
        try:
            length = int(length_str)
        except ValueError as e:
            raise LspProtocolError(f"bad Content-Length: {length_str!r}") from e
        body = stream.read(length)
        if len(body) != length:
            return None  # short read at EOF
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise LspProtocolError(f"invalid JSON body: {e}") from e

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            # Response to one of our requests.
            with self._pending_lock:
                q = self._pending.pop(msg["id"], None)
            if q is not None:
                q.put_nowait(msg)
            return
        method = msg.get("method")
        if method == "textDocument/publishDiagnostics":
            self._on_publish_diagnostics(msg.get("params") or {})
            return
        if method == "window/logMessage" or method == "window/showMessage":
            log.debug("lsp %s: %s", method, (msg.get("params") or {}).get("message"))
            return
        if "id" in msg and "method" in msg:
            # Server-to-client request — we don't implement any yet.
            # Reply with method-not-found so the server doesn't hang.
            self._write_frame(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32601, "message": "not implemented"},
                }
            )

    def _on_publish_diagnostics(self, params: dict) -> None:
        uri = params.get("uri")
        if not isinstance(uri, str):
            return
        # The server's spelling of the URI is not ours — normalise
        # before it touches any dict. See :func:`uri_key`.
        key = uri_key(uri)
        # Drop publishes for stale versions (delayed didOpen v1
        # arriving after didChange v2 reset). Servers that don't send
        # a version field land here as None and we can't filter — same
        # as before the fix, but real LSPs (and our fake server) do.
        publish_version = params.get("version")
        if isinstance(publish_version, int):
            with self._diag_lock:
                expected = self._diag_min_version.get(key)
            if expected is not None and publish_version < expected:
                return
        diags_raw = params.get("diagnostics") or []
        diags: list[Diagnostic] = []
        for d in diags_raw:
            try:
                start = d.get("range", {}).get("start", {})
                diags.append(
                    Diagnostic(
                        uri=uri,
                        severity=int(d.get("severity", 1)),
                        line=int(start.get("line", 0)),
                        character=int(start.get("character", 0)),
                        message=str(d.get("message", "")),
                        code=str(d["code"]) if "code" in d else None,
                        source=d.get("source"),
                    )
                )
            except (TypeError, ValueError):
                continue
        with self._diag_lock:
            self._diagnostics[key] = diags
            event = self._diagnostics_event.setdefault(key, threading.Event())
            event.set()
