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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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


@dataclass(frozen=True)
class NavResponse:
    """A navigation result plus who produced it.

    The provenance fields are not diagnostics-for-humans; they are what
    stops an empty ``items`` being reported as a fact about the code.
    Four different situations produce ``items == []``:

    * the answer really is empty — nothing references this symbol;
    * every server that claims the file failed (``failures``);
    * no server claims the file at all (``consulted`` empty, and no
      failures either);
    * the server is alive but still indexing (``progress``).

    Only the first is a statement about the workspace. Collapsing them
    into a bare list is precisely the bug class this engine exists to
    avoid, so the distinction is carried in the type rather than left
    to each caller to reconstruct.
    """

    items: list[Any] = field(default_factory=list)
    #: Servers that answered, by binary name.
    consulted: tuple[str, ...] = ()
    #: (binary, message) for servers that were asked and did not answer.
    failures: tuple[tuple[str, str], ...] = ()
    #: Progress reported by a server that timed out, if any.
    progress: Optional[dict] = None
    #: Configured but not started — only meaningful for workspace-wide
    #: queries, which have no path to route on.
    not_running: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.items)

    @property
    def trustworthy(self) -> bool:
        """True when an empty result can be read as "nothing found".

        False when something prevented a complete answer, which the
        caller must surface rather than round down to zero.
        """
        return (bool(self.consulted) and not self.failures
                and self.progress is None and not self.not_running)


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

    # ─── navigation ──────────────────────────────────────────────────
    #
    # Two things make this more than a passthrough to ``LspClient``.
    #
    # First, a file can route to several servers (an ``.html`` claimed
    # by both the HTML and the TypeScript server), so every answer is a
    # merge — and a merge that silently drops a server's failure is how
    # a half-answer passes for a whole one. Hence ``NavResponse``, which
    # carries *who answered* alongside the results.
    #
    # Second, LSP has no "look at this file" request. A position query
    # against a document the server has never been told about returns
    # empty, not an error, so every entry point here opens the file
    # first.

    def _ensure_open(self, path) -> tuple[LspServerSpec, ...]:
        """Open ``path`` if it is not already, and return its servers.

        Empty tuple means no configured server claims the extension —
        a normal condition, distinct from an open that failed, which
        raises.
        """
        uri = _path_to_uri(path)
        with self._lock:
            entry = self._uri_routing.get(uri)
        if entry is not None:
            return entry[1]
        p = Path(path)
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise LspError(f"cannot read {p}: {e}") from e
        if not self.did_open(p, content):
            return ()
        with self._lock:
            entry = self._uri_routing.get(uri)
        return entry[1] if entry else ()

    def _fan_out(self, specs, call, *, what: str) -> "NavResponse":
        """Run ``call`` against each server and merge, keeping track of
        which ones answered.

        A server that fails is recorded rather than raised: with two
        servers on one file, one being broken should cost its half of
        the answer and nothing more. A server that times out *while
        reporting progress* attaches that progress, because "still
        indexing, 40%" and "dead" are otherwise the same empty list.
        """
        items: list = []
        consulted: list[str] = []
        failures: list[tuple[str, str]] = []
        progress = None
        for spec in specs:
            name = os.path.basename(spec.command[0])
            try:
                client = self._client_for(spec)
            except LspError as e:
                failures.append((name, str(e)))
                continue
            try:
                items.extend(call(client) or [])
                consulted.append(name)
            except LspError as e:
                failures.append((name, str(e)))
                snap = client.progress_snapshot()
                if snap is not None and progress is None:
                    progress = dict(snap, server=name)
                log.debug("%s: %s failed: %s", what, name, e)
        return NavResponse(items=items, consulted=tuple(consulted),
                           failures=tuple(failures), progress=progress)

    # ─── symbol resolution ───────────────────────────────────────────

    def find_symbols(self, path, name: str, *,
                     kind: Optional[int] = None) -> "NavResponse":
        """Locate ``name`` in ``path`` via ``textDocument/documentSymbol``.

        This is what makes the name-addressed tools possible: a caller
        knows ``did_open``, not line 102 column 8. Matching is exact on
        the symbol name — a prefix match would silently return
        ``did_open_all`` for ``did_open``, and a caller that then renames
        it has no way to notice.

        ``container`` disambiguation is left to the caller, which is why
        every match is returned rather than the first: two classes in one
        file can both have ``start``, and picking one is a coin flip
        dressed as an answer.
        """
        specs = self._ensure_open(path)
        if not specs:
            return NavResponse(items=[])
        res = self._fan_out(
            specs, lambda c: c.document_symbols(path,
                                                timeout=self._request_timeout),
            what="documentSymbol")
        matches = [s for s in res.items
                   if s.name == name and (kind is None or s.kind == kind)]
        return NavResponse(items=matches, consulted=res.consulted,
                           failures=res.failures, progress=res.progress)

    def definition(self, path, line: int, character: int) -> "NavResponse":
        specs = self._ensure_open(path)
        return self._fan_out(
            specs,
            lambda c: c.definition(path, line, character,
                                   timeout=self._request_timeout),
            what="definition")

    def implementation(self, path, line: int, character: int) -> "NavResponse":
        specs = self._ensure_open(path)
        return self._fan_out(
            specs,
            lambda c: c.implementation(path, line, character,
                                       timeout=self._request_timeout),
            what="implementation")

    def references(self, path, line: int, character: int, *,
                   include_declaration: bool = True) -> "NavResponse":
        specs = self._ensure_open(path)
        return self._fan_out(
            specs,
            lambda c: c.references(path, line, character,
                                   include_declaration=include_declaration,
                                   timeout=self._request_timeout),
            what="references")

    def hover(self, path, line: int, character: int) -> "NavResponse":
        specs = self._ensure_open(path)
        res = self._fan_out(
            specs,
            lambda c: [c.hover(path, line, character,
                               timeout=self._request_timeout)],
            what="hover")
        # A server with nothing to say returns "", which is an answer
        # but not a result; keeping it would render as a blank hover
        # from a server that simply does not handle this file.
        return NavResponse(items=[t for t in res.items if t and t.strip()],
                           consulted=res.consulted, failures=res.failures,
                           progress=res.progress)

    def prepare_call_hierarchy(self, path, line: int,
                               character: int) -> "NavResponse":
        specs = self._ensure_open(path)
        return self._fan_out(
            specs,
            lambda c: c.prepare_call_hierarchy(path, line, character,
                                               timeout=self._request_timeout),
            what="prepareCallHierarchy")

    def calls(self, path, line: int, character: int, *,
              direction: str) -> "NavResponse":
        """Incoming or outgoing calls in one step.

        The two-request dance (prepare, then query) is an LSP detail:
        the item must be the one *that server* produced, so the pair
        cannot be split across servers. Doing both here keeps that
        invariant in one place instead of making every caller hold it.
        """
        specs = self._ensure_open(path)

        def _both(client):
            items = client.prepare_call_hierarchy(
                path, line, character, timeout=self._request_timeout)
            out = []
            for item in items:
                fn = (client.incoming_calls if direction == "incoming"
                      else client.outgoing_calls)
                out.extend(fn(item, timeout=self._request_timeout))
            return out

        return self._fan_out(specs, _both, what=f"{direction}Calls")

    def rename(self, path, line: int, character: int,
               new_name: str) -> "NavResponse":
        """Compute the rename edit. Nothing is written here.

        Applying is the caller's step, deliberately: an edit that
        touches thirty files across a workspace should be inspectable
        before it lands, and the engine is the wrong layer to decide
        that for everyone.
        """
        specs = self._ensure_open(path)
        return self._fan_out(
            specs,
            lambda c: ([c.rename(path, line, character, new_name,
                                 timeout=self._request_timeout)]
                       if c.prepare_rename(path, line, character,
                                           timeout=self._request_timeout)
                       else []),
            what="rename")

    def workspace_symbols(self, query: str, *,
                          start_all: bool = False) -> "NavResponse":
        """Search symbols across the project.

        There is no file path to route on, so there is no honest way to
        pick a server. Default is to ask the ones **already running**,
        which is fast and correct-as-far-as-it-goes; the cost is that a
        fresh session has none running and would answer "nothing found"
        for a symbol that plainly exists.

        That is why ``not_running`` is part of the response and not a
        footnote: an empty result from zero servers is not a statement
        about the workspace. ``start_all=True`` spawns every configured
        server instead — correct, and expensive enough that it must be
        asked for rather than assumed. Starting nine servers eagerly is
        the preload mistake that stopped cclsp loading at all.
        """
        with self._lock:
            running = [s for s in self._servers if s in self._clients]
        specs = list(self._servers) if start_all else running
        idle = [os.path.basename(s.command[0])
                for s in self._servers if s not in specs]
        res = self._fan_out(
            specs, lambda c: c.workspace_symbols(
                query, timeout=self._request_timeout),
            what="workspaceSymbol")
        return NavResponse(items=res.items, consulted=res.consulted,
                           failures=res.failures, progress=res.progress,
                           not_running=tuple(idle))

    def restart(self, extensions: Optional[list[str]] = None) -> list[str]:
        """Stop the servers for ``extensions`` (or all) so the next
        request starts them fresh.

        Restarting is the documented escape hatch for a wedged server,
        and it is also how a config change takes effect. Returns the
        binaries actually stopped, because "restarted 0 servers" and
        "restarted 3" are very different answers to the same request.
        """
        wanted = {e.lower().lstrip(".") for e in (extensions or [])}
        stopped: list[str] = []
        with self._lock:
            targets = [
                spec for spec in list(self._clients)
                if not wanted or wanted & {x.lower().lstrip(".")
                                           for x in spec.extensions}
            ]
            clients = [(s, self._clients.pop(s)) for s in targets]
            # Drop routing for files whose server just went away, or the
            # next did_change would be sent to a client that no longer
            # exists and fail as "did_change before did_open".
            for uri, (abs_path, specs) in list(self._uri_routing.items()):
                remaining = tuple(s for s in specs if s not in targets)
                if remaining:
                    self._uri_routing[uri] = (abs_path, remaining)
                else:
                    self._uri_routing.pop(uri, None)
        for spec, client in clients:
            try:
                client.stop(timeout=3.0)
            except Exception:  # pragma: no cover — defensive
                log.exception("error stopping %s", spec.command[0])
            stopped.append(os.path.basename(spec.command[0]))
        return stopped

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
