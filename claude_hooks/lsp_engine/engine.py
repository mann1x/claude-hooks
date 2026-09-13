"""Engine — multi-LSP routing layer.

The engine sits between the daemon (one process per project) and the
individual ``LspClient`` instances (one per language). It owns the
mapping from file extension → which LSP, lazy-starts the matching
LSP on first ``did_open``, and routes ``did_change`` / diagnostic
queries to the right child.

Phase 1 scope: routing + lifecycle. No preload, no compile-aware
mode, no per-file session bookkeeping — those land here in later
phases when there's a daemon shape to hang them off.

Threading model: the engine itself is *not* internally multi-threaded.
The daemon's IPC layer serialises calls into the engine via a single
worker (or per-session worker with cross-engine locking — that lives
in the daemon, not here). Each ``LspClient`` runs its own reader
thread; that's bounded inside the client.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from claude_hooks.lsp_engine.config import (
    EngineConfig,
    LspServerSpec,
    resolve_servers_for_path,
)
from claude_hooks.lsp_engine.lsp import (
    Diagnostic,
    LspClient,
    LspError,
)

log = logging.getLogger("claude_hooks.lsp_engine.engine")


class Engine:
    """Owns the LSP clients for a project. Thread-safe for concurrent
    callers via a single coarse lock — the daemon's IPC layer is what
    actually parallelises across sessions.
    """

    def __init__(
        self,
        project_root: str | os.PathLike,
        servers: list[LspServerSpec],
        config: Optional[EngineConfig] = None,
        *,
        startup_timeout: float = 10.0,
        request_timeout: float = 5.0,
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._servers = list(servers)
        self._config = config or EngineConfig()
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout

        # spec -> LspClient, lazily populated on first did_open that
        # routes to that spec. Identity-keyed (the spec dataclass is
        # frozen, so it's hashable) so we don't accidentally start
        # two clients for the same spec.
        self._clients: dict[LspServerSpec, LspClient] = {}
        # uri -> (abs_path, spec) the file is currently routed to.
        # Storing the path alongside the spec lets ``refresh_open_files``
        # re-read content from disk without round-tripping back through
        # ``urllib.parse`` to derive the path from the URI.
        self._uri_routing: dict[str, tuple[str, LspServerSpec]] = {}
        self._lock = threading.RLock()
        self._stopped = False

    # ─── lifecycle ───────────────────────────────────────────────────

    def shutdown(self, *, timeout: float = 3.0) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            clients = list(self._clients.items())
            self._clients.clear()
            self._uri_routing.clear()
        # Stop outside the lock so a slow shutdown doesn't block other
        # threads still observing the engine state.
        for spec, client in clients:
            try:
                client.stop(timeout=timeout)
            except Exception:  # pragma: no cover — defensive
                log.exception("error stopping LSP for %s", spec.command[0])

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    # ─── document operations ─────────────────────────────────────────

    def did_open(self, path: str | os.PathLike, content: str) -> bool:
        """Open ``path`` in whichever LSP claims its extension.

        Returns True if a server matched (and the open was forwarded),
        False if no configured server claims the file. False is *not*
        an error — most projects have files no LSP cares about (READMEs,
        JSON fixtures, etc).
        """
        specs = resolve_servers_for_path(path, self._servers)
        if not specs:
            return False
        uri = _path_to_uri(path)
        abs_path = str(Path(path).resolve())
        opened = []
        first_error: Optional[LspError] = None
        for spec in specs:
            try:
                self._client_for(spec).did_open(path, content)
                opened.append(spec)
            except LspError as e:
                # One server failing must not cost the file its others —
                # an .html handled by both an HTML and a TS server is
                # still worth half an answer.
                first_error = first_error or e
                log.warning("did_open: %s failed for %s",
                            spec.command[0], abs_path, exc_info=True)
        if not opened:
            # Every server that claimed the file failed. Returning False
            # here would report it as "no server claims this file" — a
            # perfectly normal condition for a README — and lose the
            # fact that a configured server is broken. Raise what went
            # wrong instead.
            if first_error is not None:
                raise first_error
            return False
        with self._lock:
            self._uri_routing[uri] = (abs_path, tuple(opened))
        return True

    def did_change(self, path: str | os.PathLike, content: str) -> bool:
        """Forward a content change to the LSP that owns this URI.

        Returns False if the file was never opened (or routes to no
        server), so the daemon can decide whether to skip silently or
        promote to a did_open.
        """
        uri = _path_to_uri(path)
        with self._lock:
            entry = self._uri_routing.get(uri)
        if entry is None:
            return False
        _abs_path, specs = entry
        for spec in specs:
            self._client_for(spec).did_change(path, content)
        return True

    def did_close(self, path: str | os.PathLike) -> bool:
        uri = _path_to_uri(path)
        with self._lock:
            entry = self._uri_routing.pop(uri, None)
        if entry is None:
            return False
        _abs_path, specs = entry
        ok = False
        for spec in specs:
            try:
                self._client_for(spec).did_close(path)
                ok = True
            except LspError:
                continue
        return ok

    def get_diagnostics(
        self,
        path: str | os.PathLike,
        *,
        timeout: float = 2.0,
    ) -> list[Diagnostic]:
        uri = _path_to_uri(path)
        with self._lock:
            entry = self._uri_routing.get(uri)
        if entry is None:
            return []
        _abs_path, specs = entry
        if len(specs) == 1:
            return self._client_for(specs[0]).get_diagnostics(
                path, timeout=timeout)
        # Several servers claim this file — an .html carrying JS and
        # CSS, say. Each holds its own view, so the answer is the
        # union. The timeout is per server rather than shared: a slow
        # one should not consume the budget of the rest, and the
        # callers here already bound the whole call.
        merged: list[Diagnostic] = []
        for spec in specs:
            try:
                merged.extend(
                    self._client_for(spec).get_diagnostics(
                        path, timeout=timeout))
            except LspError:
                log.debug("get_diagnostics: %s failed", spec.command[0])
        return merged

    def refresh_open_files(self) -> int:
        """Re-send the on-disk content of every open file to its LSP.

        Used after a git branch switch when disk content has diverged
        from the LSP's in-memory copy. Returns the count refreshed.
        Files that no longer exist on disk (deleted by the branch
        switch) are silently dropped from the routing so their next
        ``did_change`` would no-op rather than error.
        """
        refreshed = 0
        with self._lock:
            snapshot = dict(self._uri_routing)
        for uri, (abs_path, specs) in snapshot.items():
            p = Path(abs_path)
            if not p.is_file():
                with self._lock:
                    self._uri_routing.pop(uri, None)
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for spec in specs:
                try:
                    self._client_for(spec).did_change(abs_path, content)
                    refreshed += 1
                except LspError:  # pragma: no cover — defensive
                    log.exception("refresh: did_change failed for %s (%s)",
                                  abs_path, spec.command[0])
        return refreshed

    # ─── introspection ───────────────────────────────────────────────

    def open_files(self) -> list[str]:
        """Return the URIs currently routed to *some* LSP.

        Useful for the daemon's ``status`` op and for tests verifying
        ``did_close`` actually drops the routing entry.
        """
        with self._lock:
            return list(self._uri_routing.keys())

    def active_servers(self) -> list[LspServerSpec]:
        with self._lock:
            return list(self._clients.keys())

    def support_report(self) -> list[dict]:
        """What each configured server claims, and what it can do.

        Two different questions, and the config only answers the first.
        Extensions say what the engine will *route* — they remain the
        honest statement of what is supported, and are how an operator
        reads the config at a glance. Capabilities say what the server
        will *do* once routed, and only the server can answer that,
        after it has started.

        ``running`` distinguishes them: a configured-but-never-started
        server has extensions and no capabilities, which is a claim
        without evidence rather than a contradiction.
        """
        out: list[dict] = []
        with self._lock:
            clients = dict(self._clients)
        for spec in self._servers:
            client = clients.get(spec)
            entry = {
                "command": list(spec.command),
                "extensions": sorted(spec.extensions),
                "running": client is not None,
                "capabilities": [],
                "pull_diagnostics": False,
                "stderr_tail": [],
            }
            if client is not None:
                entry["capabilities"] = sorted(client.server_capabilities)
                entry["pull_diagnostics"] = client.supports_pull_diagnostics
                # The one channel a degraded server has. bash-language-
                # server without shellcheck says so here and nowhere
                # else — its LSP conversation looks perfectly healthy.
                entry["stderr_tail"] = client.stderr_tail()[-5:]
            out.append(entry)
        return out

    def servers_for_path(self, path) -> list[list[str]]:
        """Which servers would claim ``path`` — all of them, in order.

        Answers "is this file supported, and by what" without opening
        it, which is the question an operator actually asks.
        """
        return [list(s.command)
                for s in resolve_servers_for_path(path, self._servers)]

    def configured_extensions(self) -> set[str]:
        """All extensions any configured server claims, lowercased and
        no leading dot. Used by the preload step to skip files whose
        language has no LSP attached.
        """
        out: set[str] = set()
        for spec in self._servers:
            out.update(spec.extensions)
        return out

    # ─── internals ───────────────────────────────────────────────────

    def _client_for(self, spec: LspServerSpec) -> LspClient:
        """Return the running client for ``spec``, lazy-starting it
        on first call. Holds the engine lock across the start so two
        concurrent ``did_open`` calls for the same language don't
        race-spawn two LSP processes.
        """
        with self._lock:
            if self._stopped:
                raise LspError("engine has been shut down")
            client = self._clients.get(spec)
            if client is not None:
                return client
            log.info(
                "lsp_engine: starting %s for %s",
                spec.command[0],
                ",".join(spec.extensions),
            )
            client = LspClient(
                command=list(spec.command),
                root_dir=self._resolve_root(spec),
                startup_timeout=self._startup_timeout,
                request_timeout=self._request_timeout,
            )
            try:
                client.start()
            except LspError:
                # Failed start: don't cache it, re-raise so the caller
                # gets a clear error. Next call retries from scratch.
                raise
            self._clients[spec] = client
            return client

    def _resolve_root(self, spec: LspServerSpec) -> Path:
        # rootDir in cclsp.json is relative to the project root by
        # convention. "." → the project root. An absolute path wins
        # outright (rare but supported).
        if os.path.isabs(spec.root_dir):
            return Path(spec.root_dir)
        return (self._project_root / spec.root_dir).resolve()


def _path_to_uri(path: str | os.PathLike) -> str:
    return Path(path).resolve().as_uri()
