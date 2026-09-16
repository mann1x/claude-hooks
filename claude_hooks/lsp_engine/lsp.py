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

The navigation surface — definition, references, implementation, hover,
document/workspace symbols, call hierarchy and rename — sits alongside
it. Every one of those returns a union type; decoding lives in
:mod:`claude_hooks.lsp_engine.protocol` as pure functions, so the arms
belonging to servers we do not have installed are still covered by
tests. The methods here are the transport half only.
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
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Optional
from urllib.parse import unquote

from . import protocol
from .protocol import (
    CallHierarchyCall,
    CallHierarchyItem,
    Location,
    Symbol,
    WorkspaceEdit,
)

log = logging.getLogger("claude_hooks.lsp_engine.lsp")

#: Declared to servers so they may use the full SymbolKind range. A
#: server clamps to what the client lists, so an omitted kind comes back
#: as a different kind rather than as an error.
_SYMBOL_KIND_VALUE_SET = sorted(protocol.SYMBOL_KINDS)

#: How many stderr lines to keep per server for diagnosis. Small on
#: purpose — this is a breadcrumb for "why is this server silent", not
#: a log file. The full stream goes to the engine log.
_STDERR_TAIL_LINES = 40

#: Substrings that mark a stderr line as worth surfacing at WARNING.
#: A degraded server announces itself in prose here or not at all.
_STDERR_ALERT_HINTS = (
    "not found", "not installed", "missing", "cannot find", "couldn't find",
    "unable to", "failed", "error", "deprecat", "no such file",
)


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
    "hxx": "cpp",
    # CUDA. clangd handles .cu/.cuh, but they were absent from every
    # config until 2026-09-16, so CUDA files had no server at all — and
    # an unmapped extension is a silent no-diagnostics, not an error.
    "cu": "cuda",
    "cuh": "cuda",
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
    # Web trio, served by vscode-langservers-extracted. `html` is NOT
    # mapped to a JS languageId: tsserver silently declines a document
    # announced as anything it doesn't claim, so pointing html at it
    # would buy an accepted file and an empty diagnostic list — the
    # failure shape this module exists to prevent. Embedded <script>
    # analysis is a client-side virtual-document trick in VS Code, not
    # something a standalone server does.
    "html": "html",
    "htm": "html",
    "css": "css",
    "scss": "scss",
    "less": "less",
    "json": "json",
    "jsonc": "jsonc",
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


def _call_item_wire(item: CallHierarchyItem) -> dict:
    """A ``CallHierarchyItem`` back on the wire.

    The incoming/outgoing requests take the item the *server* handed us
    in ``prepareCallHierarchy``, so this has to round-trip faithfully:
    some servers (rust-analyzer, jdtls) key their internal lookup on the
    exact range they sent, and a reconstructed-but-different item comes
    back as an empty call list rather than an error.
    """
    def _r(r) -> dict:
        return {"start": {"line": r.start.line, "character": r.start.character},
                "end": {"line": r.end.line, "character": r.end.character}}

    wire = {
        "name": item.name,
        "kind": item.kind,
        "uri": item.uri,
        "range": _r(item.range),
        "selectionRange": _r(item.selection),
    }
    if item.detail:
        wire["detail"] = item.detail
    return wire


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
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()

        #: What the server said it can do, from the ``initialize``
        #: result. Previously validated and thrown away, which left the
        #: engine unable to answer the most basic question about a
        #: server it had just started.
        self._server_capabilities: dict = {}

        #: Last few stderr lines. A server that is degraded rather than
        #: broken says so here and nowhere else — there is no LSP
        #: message for "I started fine but a helper binary is missing".
        self._stderr_tail: deque = deque(maxlen=_STDERR_TAIL_LINES)

        #: In-flight ``$/progress`` work, keyed by token. Bounded by the
        #: server's own begin/end pairs rather than by us, which is safe
        #: because a token that never ends belongs to a server that is
        #: still claiming to work — exactly what we want to report.
        self._progress: dict = {}
        self._progress_lock = threading.Lock()

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

        # Drain stderr. Two reasons, and the second is the serious one.
        # (1) A server that is *degraded* reports it here and nowhere
        #     else — bash-language-server without shellcheck starts,
        #     handshakes, advertises its capabilities and then publishes
        #     an empty diagnostic list forever, which is byte-identical
        #     to "this file is clean". Its complaint goes to stderr.
        # (2) An undrained PIPE is a deadlock. The buffer is ~64 KB on
        #     Linux; a server that exceeds it blocks on write, and since
        #     these are single-threaded event loops it stops answering
        #     LSP entirely — presenting as a hung server with no error.
        #     rust-analyzer and gopls are both chatty there.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name=f"lsp-stderr-{self._command[0]}",
            daemon=True,
        )
        self._stderr_thread.start()

        # Initialize handshake — required before any other request.
        # The deadline applies to the whole handshake, not each step.
        deadline = time.monotonic() + self._startup_timeout
        result = self._send_request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self._root_dir.as_uri(),
                # `rootUri` has been deprecated since LSP 3.6 in favour
                # of `workspaceFolders`, and pyright reads only the
                # latter when deciding where its `pyrightconfig.json`
                # is. With rootUri alone it starts, handshakes, answers
                # every request — and resolves no first-party import, so
                # `find_references` returns just the matches inside the
                # file you asked about. A shorter list, not an error,
                # which is indistinguishable from a symbol that really
                # has one reference.
                "workspaceFolders": [{
                    "uri": self._root_dir.as_uri(),
                    "name": self._root_dir.name,
                }],
                "capabilities": {
                    "textDocument": {
                        "synchronization": {
                            "didSave": False,
                            "willSave": False,
                        },
                        "publishDiagnostics": {
                            "relatedInformation": False,
                            # We already drop publishes older than the
                            # last didChange; saying so lets servers
                            # stamp the version rather than guess.
                            "versionSupport": True,
                            "codeDescriptionSupport": False,
                            "dataSupport": False,
                        },
                        # LSP 3.17 pull diagnostics. A server only
                        # advertises ``diagnosticProvider`` when the
                        # *client* declares support — so omitting this
                        # guaranteed every server looked push-only, and
                        # the engine had no choice but to wait out a
                        # timeout and call the silence an answer.
                        "diagnostic": {
                            "dynamicRegistration": False,
                            "relatedDocumentSupport": False,
                        },
                        # ─── navigation ──────────────────────────────
                        # Same rule as pull diagnostics above: a server
                        # only advertises a provider when the client
                        # declares the matching capability, so omitting
                        # any of these makes the feature look absent
                        # rather than undeclared.
                        #
                        # ``linkSupport`` opts into ``LocationLink``,
                        # whose ``targetSelectionRange`` points at the
                        # *name* instead of the whole definition body —
                        # strictly better answers, and the reason
                        # ``parse_locations`` reads both shapes.
                        "definition": {"linkSupport": True},
                        "typeDefinition": {"linkSupport": True},
                        "implementation": {"linkSupport": True},
                        "references": {"dynamicRegistration": False},
                        "hover": {
                            "contentFormat": ["markdown", "plaintext"],
                        },
                        "documentSymbol": {
                            # Without this a server may flatten to
                            # SymbolInformation, losing the nesting that
                            # tells `Engine.start` from `Client.start`.
                            "hierarchicalDocumentSymbolSupport": True,
                            "symbolKind": {"valueSet": _SYMBOL_KIND_VALUE_SET},
                        },
                        "callHierarchy": {"dynamicRegistration": False},
                        "rename": {
                            # prepareSupport lets us ask "is this
                            # renameable, and what is its extent?"
                            # before editing anything.
                            "prepareSupport": True,
                            "dynamicRegistration": False,
                        },
                    },
                    "workspace": {
                        "symbol": {
                            "symbolKind": {"valueSet": _SYMBOL_KIND_VALUE_SET},
                        },
                        "workspaceEdit": {
                            "documentChanges": True,
                            # Deliberately NOT declaring
                            # resourceOperations. A server that believes
                            # we can create/rename/delete files will
                            # emit those operations as part of a rename
                            # (jdtls renames the file holding a renamed
                            # public class), and we do not apply file
                            # operations. Not declaring it means the
                            # server keeps the rename to text edits;
                            # `WorkspaceEdit.file_operations` still
                            # reports any that arrive anyway, so the
                            # caller learns the edit was partial rather
                            # than being told it succeeded.
                            "failureHandling": "abort",
                        },
                    },
                    # Servers only emit `$/progress` when the client
                    # says it can receive it. cclsp left this off and
                    # ignored the notification, which is why "still
                    # indexing" and "dead" were the same observation.
                    "window": {"workDoneProgress": True},
                    "general": {
                        "positionEncodings": ["utf-16"],
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
        self._server_capabilities = result.get("capabilities") or {}
        log.debug("lsp %s capabilities: %s", self._command[0],
                  sorted(self._server_capabilities))
        self._send_notification("initialized", {})

    # ─── introspection ───────────────────────────────────────────────

    @property
    def server_capabilities(self) -> dict:
        """What the server advertised at ``initialize``.

        Empty until :meth:`start` completes. This is the only place a
        server states what it can do; there is no message for what file
        types it handles — that association is always the client's, which
        is why ``cclsp.json`` exists.
        """
        return dict(self._server_capabilities)

    def supports(self, capability: str) -> bool:
        """Is ``capability`` advertised (and not explicitly False)?"""
        val = self._server_capabilities.get(capability)
        return bool(val) if not isinstance(val, dict) else True

    @property
    def supports_pull_diagnostics(self) -> bool:
        """Does the server implement ``textDocument/diagnostic``?

        When true the engine can *ask* for diagnostics instead of
        waiting for a push it cannot distinguish from silence.
        """
        return bool(self._server_capabilities.get("diagnosticProvider"))

    def stderr_tail(self) -> list[str]:
        """Recent stderr lines — where a degraded server explains
        itself, since the protocol gives it nowhere else to."""
        return list(self._stderr_tail)

    def _drain_stderr(self) -> None:
        """Read the child's stderr until EOF.

        Never lets the pipe fill (see the comment at the spawn site),
        keeps a bounded tail for diagnosis, and promotes lines that look
        like complaints to WARNING so a degraded server is visible in
        the engine log rather than only in its absence of output.
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        name = os.path.basename(self._command[0])
        try:
            for raw in iter(proc.stderr.readline, b""):
                if self._stop_requested.is_set():
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                low = line.lower()
                if any(h in low for h in _STDERR_ALERT_HINTS):
                    log.warning("lsp %s stderr: %s", name, line[:400])
                else:
                    log.debug("lsp %s stderr: %s", name, line[:400])
        except (OSError, ValueError):
            # Pipe closed underneath us during shutdown — expected.
            pass

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
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=1.0)
                self._stderr_thread = None

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

        # Prefer asking over waiting. With push diagnostics an empty
        # result and a server that never answers are the same
        # observation — we wait out the timeout and return [], which the
        # caller renders as "no problems". Pull diagnostics (LSP 3.17)
        # turn that into a question with an answer, and a failure into
        # an exception instead of a plausible silence. Only available
        # because the client now declares `textDocument.diagnostic`;
        # servers withhold `diagnosticProvider` otherwise.
        if self.supports_pull_diagnostics:
            pulled = self._pull_diagnostics(path, key, timeout=timeout)
            if pulled is not None:
                return pulled

        if not event.wait(timeout=timeout):
            return []
        with self._diag_lock:
            return list(self._diagnostics.get(key, []))

    def _pull_diagnostics(
        self, path, key: str, *, timeout: float,
    ) -> Optional[list[Diagnostic]]:
        """``textDocument/diagnostic`` round trip.

        Returns None — never an empty list — when the pull could not be
        completed, so the caller falls back to the push path rather than
        treating a transport problem as a clean file.
        """
        try:
            res = self._send_request(
                "textDocument/diagnostic",
                {"textDocument": {"uri": path_to_uri(path)}},
                timeout=timeout,
            )
        except (LspError, OSError) as e:
            log.debug("pull diagnostics unavailable for %s: %s", key, e)
            return None
        if not isinstance(res, dict):
            return None
        # A full report carries items; an unchanged report means "same
        # as last time", which we cannot answer from here.
        kind = res.get("kind")
        if kind == "unchanged":
            with self._diag_lock:
                cached = self._diagnostics.get(key)
            return list(cached) if cached is not None else None
        items = res.get("items")
        if items is None:
            return None
        diags = self._parse_diagnostics(key, items)
        with self._diag_lock:
            self._diagnostics[key] = diags
            self._diagnostics_event.setdefault(key, threading.Event()).set()
        return list(diags)

    # ─── navigation ──────────────────────────────────────────────────
    #
    # All positions crossing this boundary are LSP-native: 0-based line
    # *and* 0-based character. The MCP surface is 1-based on both axes,
    # and converting anywhere other than at that one edge is how a
    # result ends up one line off in a way nobody notices until it lands
    # on the wrong function.

    def _position_params(self, path, line: int, character: int) -> dict:
        return {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character},
        }

    def _request_at(self, method: str, path, line: int, character: int,
                    *, timeout: Optional[float] = None,
                    extra: Optional[dict] = None) -> Any:
        params = self._position_params(path, line, character)
        if extra:
            params.update(extra)
        return self._send_request(method, params, timeout=timeout)

    def definition(self, path, line: int, character: int,
                   *, timeout: Optional[float] = None) -> list[Location]:
        return protocol.parse_locations(
            self._request_at("textDocument/definition", path, line, character,
                             timeout=timeout))

    def type_definition(self, path, line: int, character: int,
                        *, timeout: Optional[float] = None) -> list[Location]:
        return protocol.parse_locations(
            self._request_at("textDocument/typeDefinition", path, line,
                             character, timeout=timeout))

    def implementation(self, path, line: int, character: int,
                       *, timeout: Optional[float] = None) -> list[Location]:
        return protocol.parse_locations(
            self._request_at("textDocument/implementation", path, line,
                             character, timeout=timeout))

    def references(self, path, line: int, character: int,
                   *, include_declaration: bool = True,
                   timeout: Optional[float] = None) -> list[Location]:
        return protocol.parse_locations(self._request_at(
            "textDocument/references", path, line, character, timeout=timeout,
            extra={"context": {"includeDeclaration": include_declaration}}))

    def hover(self, path, line: int, character: int,
              *, timeout: Optional[float] = None) -> str:
        return protocol.parse_hover(
            self._request_at("textDocument/hover", path, line, character,
                             timeout=timeout))

    def document_symbols(self, path,
                         *, timeout: Optional[float] = None) -> list[Symbol]:
        uri = path_to_uri(path)
        return protocol.parse_document_symbols(
            self._send_request("textDocument/documentSymbol",
                               {"textDocument": {"uri": uri}}, timeout=timeout),
            uri=uri)

    def workspace_symbols(self, query: str,
                          *, timeout: Optional[float] = None) -> list[Symbol]:
        return protocol.parse_workspace_symbols(
            self._send_request("workspace/symbol", {"query": query},
                               timeout=timeout))

    def prepare_call_hierarchy(
        self, path, line: int, character: int,
        *, timeout: Optional[float] = None,
    ) -> list[CallHierarchyItem]:
        return protocol.parse_call_hierarchy_items(
            self._request_at("textDocument/prepareCallHierarchy", path, line,
                             character, timeout=timeout))

    def incoming_calls(self, item: CallHierarchyItem,
                       *, timeout: Optional[float] = None
                       ) -> list[CallHierarchyCall]:
        return protocol.parse_calls(
            self._send_request("callHierarchy/incomingCalls",
                               {"item": _call_item_wire(item)}, timeout=timeout),
            direction="incoming")

    def outgoing_calls(self, item: CallHierarchyItem,
                       *, timeout: Optional[float] = None
                       ) -> list[CallHierarchyCall]:
        return protocol.parse_calls(
            self._send_request("callHierarchy/outgoingCalls",
                               {"item": _call_item_wire(item)}, timeout=timeout),
            direction="outgoing")

    def prepare_rename(self, path, line: int, character: int,
                       *, timeout: Optional[float] = None) -> bool:
        """Is the symbol at this position renameable?

        Returns True when the server says yes *or* when it does not
        implement the check — an unimplemented precondition must not
        read as a refusal. Only an explicit ``null`` is a no.
        """
        if not self.supports("renameProvider"):
            return True
        provider = self._server_capabilities.get("renameProvider")
        if not (isinstance(provider, dict) and provider.get("prepareProvider")):
            return True
        try:
            res = self._request_at("textDocument/prepareRename", path, line,
                                   character, timeout=timeout)
        except LspError as e:
            log.debug("prepareRename unavailable: %s", e)
            return True
        if res is None:
            return False
        # {defaultBehavior: false} is the server declining in the one
        # shape that is not null.
        if isinstance(res, dict) and res.get("defaultBehavior") is False:
            return False
        return True

    def rename(self, path, line: int, character: int, new_name: str,
               *, timeout: Optional[float] = None) -> WorkspaceEdit:
        return protocol.parse_workspace_edit(self._request_at(
            "textDocument/rename", path, line, character, timeout=timeout,
            extra={"newName": new_name}))

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
            # Tell the server to stop. Without this the request keeps
            # running — a `find_references` over a large workspace can
            # occupy a single-threaded server for minutes after we have
            # given up on it, so the *next* request queues behind work
            # nobody is waiting for and times out in turn. That is how
            # one slow call degrades into a server that looks hung.
            #
            # Best-effort by definition: the server may answer anyway,
            # and the reply is discarded because `rid` is no longer
            # pending.
            try:
                self._send_notification("$/cancelRequest", {"id": rid})
            except (LspError, OSError) as e:      # pragma: no cover - rare
                log.debug("could not cancel request %s (%s): %s", rid, method, e)
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
        if method == "$/progress":
            self._on_progress(msg.get("params") or {})
            return
        if method == "window/logMessage" or method == "window/showMessage":
            log.debug("lsp %s: %s", method, (msg.get("params") or {}).get("message"))
            return
        if "id" in msg and "method" in msg:
            self._answer_server_request(msg, method)

    def _answer_server_request(self, msg: dict, method) -> None:
        """Reply to a server-to-client request.

        Three of these must be answered *successfully* rather than with
        method-not-found, because the error is not neutral — a server
        that asks and is refused changes its behaviour:

        ``window/workDoneProgress/create``
            We declared ``window.workDoneProgress``, so refusing the
            token we just asked to be sent is incoherent, and a server
            that takes the error seriously stops reporting progress —
            re-creating the exact blind spot the declaration was added
            to remove.
        ``workspace/configuration``
            gopls and pyright ask for their settings on startup. An
            error there is not "no settings", it is a failed request
            during initialisation, and both degrade quietly afterwards.
            A list of nulls is the spec's way of saying "defaults".
        ``client/registerCapability``
            Dynamic registration. We do not track registrations, but
            acknowledging is correct: the server is informing us, not
            asking permission.
        """
        rid = msg["id"]
        if method == "workspace/configuration":
            items = (msg.get("params") or {}).get("items")
            n = len(items) if isinstance(items, list) else 1
            result: Any = [None] * n
        elif method in ("window/workDoneProgress/create",
                        "client/registerCapability",
                        "client/unregisterCapability"):
            result = None
        else:
            self._write_frame({
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "not implemented"},
            })
            return
        self._write_frame({"jsonrpc": "2.0", "id": rid, "result": result})

    # ─── progress ────────────────────────────────────────────────────

    def _on_progress(self, params: dict) -> None:
        """Track the server's own account of what it is doing.

        This is the difference between "timed out" and "timed out while
        clangd was 40% through indexing 12k files". cclsp had zero
        references to ``$/progress``, so every slow answer and every
        dead server produced the same message.
        """
        value = params.get("value")
        if not isinstance(value, dict):
            return
        kind = value.get("kind")
        token = params.get("token")
        with self._progress_lock:
            if kind == "end":
                self._progress.pop(token, None)
                return
            if kind not in ("begin", "report"):
                return
            prev = self._progress.get(token) or {}
            pct = value.get("percentage")
            self._progress[token] = {
                "title": value.get("title") or prev.get("title") or "",
                "message": value.get("message") or prev.get("message") or "",
                "percentage": pct if isinstance(pct, (int, float)) else prev.get("percentage"),
                "since": prev.get("since") or time.monotonic(),
            }

    def progress_snapshot(self) -> Optional[dict]:
        """The most advanced in-flight progress, or None if idle.

        Includes ``eta_seconds`` when the server reports a percentage,
        derived from elapsed-versus-percentage. That is a rough figure
        and labelled as one — but "roughly two minutes" is a decision a
        caller can act on, and a bare timeout is not.
        """
        with self._progress_lock:
            entries = list(self._progress.values())
        if not entries:
            return None
        best = max(entries, key=lambda e: e.get("percentage") or 0)
        elapsed = max(1.0, time.monotonic() - best["since"])
        pct = best.get("percentage")
        eta = None
        if isinstance(pct, (int, float)) and 0 < pct < 100:
            eta = max(1, int(elapsed * (100 - pct) / pct))
        return {
            "title": best["title"],
            "message": best["message"],
            "percentage": pct,
            "elapsed_seconds": int(elapsed),
            "eta_seconds": eta,
        }

    @staticmethod
    def _parse_diagnostics(uri: str, raw: list) -> list[Diagnostic]:
        """Build :class:`Diagnostic` objects from the wire form.

        Shared by the push notification and the pull response — the two
        carry the same item shape, and letting them drift would mean a
        server's diagnostics rendering differently depending on which
        way they arrived.
        """
        out: list[Diagnostic] = []
        for d in raw or []:
            try:
                start = d.get("range", {}).get("start", {})
                out.append(
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
            except (TypeError, ValueError, AttributeError):
                continue
        return out

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
        diags = self._parse_diagnostics(uri, params.get("diagnostics") or [])
        with self._diag_lock:
            self._diagnostics[key] = diags
            event = self._diagnostics_event.setdefault(key, threading.Event())
            event.set()
