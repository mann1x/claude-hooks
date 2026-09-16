"""MCP server exposing the claude-hooks LSP engine.

Replaces the third-party ``cclsp`` binary behind the ``lsp`` MCP tools.
The tool surface is identical (see :mod:`.tools`); what changes is what
happens underneath, and specifically the four failure modes that made
cclsp unusable on 2026-09-16 — every one of which rendered as a
plausible success rather than an error:

============================  =====================================
cclsp                         here
============================  =====================================
framed LSP by character       ``LspClient._read_frame`` reads exactly
count against a byte          ``Content-Length`` **bytes** from a
``Content-Length``; the       binary stream and decodes after.
first ``→`` in a clangd       Asserted in
hover desynced the buffer     ``tests/test_lsp_framing_multibyte.py``.
permanently.
--------------------------------------------------------------------
preloaded every server at      nothing starts until a request needs
startup against a hardcoded    it. ``find_workspace_symbols`` — the
3 s readiness race, which      one tool with no file to route on —
most servers never win.        asks only what is already running and
cline could not load the       *says so*, rather than starting nine
MCP at all.                    servers to answer one query.
--------------------------------------------------------------------
never exited on stdin EOF,     stdin EOF is the shutdown signal, and
so every closed client left    every engine is stopped on the way
orphans holding their          out. Plus an idle reaper for servers
language servers — six on      nobody has queried for
solidpc, oldest 80 days,       ``LSP_MCP_IDLE_HOURS`` (default 24).
143 MB.
--------------------------------------------------------------------
ignored ``$/progress``         progress is captured and folded into
entirely, so "still            the reply, with an ETA and a retry
indexing" and "dead" were      hint, so a slow answer is
the same message.              distinguishable from a dead one.
============================  =====================================

Two further gaps cclsp had that are closed here:

**One server, many projects.** cclsp reads its config once at startup
and serves a single workspace, so a file outside it gets "No LSP server
configured for file" — which is what a genuinely unsupported extension
also says. Here the project is resolved *per request* from the file
path, and each gets its own engine.

**Config changes take effect.** cclsp had to be killed to re-read
``cclsp.json``. Here the file's mtime is checked per request and the
affected engine is rebuilt, so ``sync_cclsp.py --write`` is enough.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from claude_hooks.lsp_engine.config import (
    _ROOT_MARKERS,
    boundary_root_for,
    describe_scope,
    find_project_root,
    candidate_cclsp_paths as _candidate_cclsp_paths,
    resolve_cclsp_path as _resolve_cclsp_path,
    CclspConfigError,
    load_cclsp_config,
)
from claude_hooks.lsp_engine.client import connect_or_spawn
from claude_hooks.lsp_engine.engine import NavResponse
from claude_hooks.lsp_mcp import tools as T
from claude_hooks.lsp_mcp.staleness import DETECTOR
from claude_hooks.mcp_stdio import force_utf8_stdio

log = logging.getLogger("claude_hooks.lsp_mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "claude-hooks-lsp"
SERVER_VERSION = "1.0.0"

def candidate_config_paths(root: Path) -> list[Path]:
    """Delegates to the engine's resolver, which the daemon also uses.

    These used to be separate implementations in opposite orders, so the
    MCP could validate one file while the daemon served another.
    """
    return _candidate_cclsp_paths(root)


def resolve_config_path(root: Path) -> Optional[Path]:
    return _resolve_cclsp_path(root)


class _ProjectEngine:
    """One engine plus the bookkeeping that keeps it honest."""

    def __init__(self, root: Path, engine: "DaemonEngine",
                 config_path: Optional[Path],
                 config_mtime: Optional[float]):
        self.root = root
        self.engine = engine
        self.config_path = config_path
        self.config_mtime = config_mtime
        self.last_used = time.monotonic()

    def config_changed(self) -> bool:
        if self.config_path is None:
            return False
        try:
            return self.config_path.stat().st_mtime != self.config_mtime
        except OSError:
            # Config deleted underneath us. Keep serving what we have
            # rather than tearing down working servers on a transient
            # error — a missing config is reported when it is next read.
            return False


class DaemonEngine:
    """The daemon's Engine, reached over its socket.

    The MCP server used to construct an ``Engine`` in-process. That
    meant two engines per project — this one and the daemon's, which
    the PostToolUse hook talks to — and therefore two fleets of
    language servers indexing the same tree, two warm-ups to pay, and
    two diagnostic caches free to disagree about the same file. On this
    host that was 12 servers across three fleets holding 357 MB.

    One engine per project was the design; this makes the MCP use it.
    Sharing the cache is also what makes de-duplication between the
    hook and the MCP possible at all — they finally see the same state.

    The method surface deliberately mirrors ``Engine``'s, so the tool
    layer above does not know or care which side of a socket it is on.
    """

    def __init__(self, root: Path, client) -> None:
        self._root = root
        self._client = client
        self._pending: list[str] = []
        # How many project entries hold this connection. One daemon now
        # serves a whole repository, so every package inside it shares
        # this handle — see ``EngineRegistry._connection_for``.
        self._holders = 1
        self._closed = False

    def acquire(self) -> "DaemonEngine":
        self._holders += 1
        return self

    @property
    def root(self) -> Path:
        return self._root

    @property
    def closed(self) -> bool:
        """True once the last holder detached."""
        return self._closed

    def add_notice(self, text: str) -> None:
        """Queue something the next tool call should surface."""
        self._pending.append(text)

    # -- navigation ----------------------------------------------------

    def find_symbols(self, path, name: str, *, kind=None, substring=True):
        return self._client.nav("find_symbols", path=str(path), name=name,
                                kind=kind, substring=substring)

    def definition(self, path, line: int, character: int):
        return self._client.nav("definition", path=str(path), line=line,
                                character=character)

    def implementation(self, path, line: int, character: int):
        return self._client.nav("implementation", path=str(path), line=line,
                                character=character)

    def references(self, path, line: int, character: int, *,
                   include_declaration: bool = True, seed: bool = True):
        return self._client.nav("references", path=str(path), line=line,
                                character=character,
                                include_declaration=include_declaration,
                                seed=seed)

    def hover(self, path, line: int, character: int):
        return self._client.nav("hover", path=str(path), line=line,
                                character=character)

    def prepare_call_hierarchy(self, path, line: int, character: int):
        return self._client.nav("prepare_call_hierarchy", path=str(path),
                                line=line, character=character)

    def calls(self, path, line: int, character: int, *, direction: str):
        return self._client.nav("calls", path=str(path), line=line,
                                character=character, direction=direction)

    def rename(self, path, line: int, character: int, new_name: str):
        return self._client.nav("rename", path=str(path), line=line,
                                character=character, new_name=new_name)

    def workspace_symbols(self, query: str, *, start_all: bool = False):
        return self._client.nav("workspace_symbols", query=query,
                                start_all=start_all)

    # -- documents and diagnostics -------------------------------------

    def did_open(self, path, content: str) -> bool:
        return self._client.did_open(path, content)

    def get_diagnostics_result(self, path, *, timeout=None):
        kw = {} if timeout is None else {"diag_timeout_s": float(timeout)}
        return self._client.diagnostics_result(path, **kw)

    def restart(self, extensions=None) -> list[str]:
        return self._client.restart(extensions)

    def reload(self, *, config: bool = True) -> dict:
        """Stop every server in the repository and re-read the config.

        Lifecycle is what this shim is *for*. Everything else it does
        is a pass-through to the daemon, but the daemon is a long-lived
        process holding a configuration it parsed once, and nothing in
        a session could replace that — which is why applying a
        ``cclsp.json`` edit meant closing the connection.
        """
        return self._client.reload(config=config)

    def take_notices(self) -> list[str]:
        """Everything worth telling the caller, once each.

        The daemon's stale-code notice, plus anything queued when this
        engine was built — a config disagreement, say.
        """
        out, self._pending = list(self._pending), []
        notice = self._client.take_stale_notice()
        if isinstance(notice, str) and notice:
            out.append(notice)
        return out

    # -- lifecycle ------------------------------------------------------

    def shutdown(self, *, timeout: float = 3.0) -> None:
        """Detach, and leave the daemon running.

        An in-process engine was ours to stop. This one is shared with
        the PostToolUse hook and any other session in the project, so
        reaping our handle must not take their servers down with it —
        the daemon has its own idle reaper for that.

        It is also shared *within* this process now. The daemon is keyed
        at the repository, so every package under it resolves to the
        same socket — and every client from this process attaches with
        the same session id, ``lsp-mcp-<pid>``. Detaching on the first
        package reaped would release the session's locks for all of
        them, which the daemon cannot distinguish from the session
        going away. So the last holder detaches, not the first.
        """
        self._holders -= 1
        if self._holders > 0:
            return
        self._closed = True
        try:
            self._client.detach()
        finally:
            self._client.close()


#: How long an unused project engine is kept attached.
DEFAULT_IDLE_HOURS = 24.0


class EngineRegistry:
    """Engines keyed by project root, built on demand.

    Thread-safe because the HTTP transport (and any future concurrent
    client) shares one registry; the stdio loop is single-threaded but
    the reaper is not.
    """

    def __init__(self, *, idle_hours: float = DEFAULT_IDLE_HOURS):
        self._engines: dict[Path, _ProjectEngine] = {}
        #: repository boundary -> the one connection serving it. The
        #: daemon is keyed there, so two packages in one repo are two
        #: entries above and one socket here.
        self._connections: dict[Path, "DaemonEngine"] = {}
        self._lock = threading.RLock()
        self._idle_seconds = max(60.0, idle_hours * 3600.0)

    def for_path(self, file_path: str | os.PathLike) -> _ProjectEngine:
        root = find_project_root(file_path)
        if root is None:
            raise T.ToolError(
                f"No project root found above {file_path}. Looked for "
                f"{', '.join(_ROOT_MARKERS)} walking up from that path. "
                f"Without a root there is nothing to configure a language "
                f"server from.")
        with self._lock:
            entry = self._engines.get(root)
            if entry is not None and entry.config_changed():
                log.info("cclsp.json changed for %s — rebuilding engine", root)
                self._drop(root)
                entry = None
            if entry is None:
                entry = self._build(root)
                self._engines[root] = entry
            entry.last_used = time.monotonic()
            return entry

    def _build(self, root: Path) -> _ProjectEngine:
        # Resolve the config where the DAEMON resolves it: at the
        # repository boundary. ``root`` here is the package, and a
        # monorepo keeps one cclsp.json at the top — so looking beside
        # the package finds nothing and this refuses to serve a project
        # the daemon would have served fine. Same file, two resolvers,
        # which is the shape of every silent disagreement this engine
        # has shipped.
        boundary = boundary_root_for(root) or root
        config_path = resolve_config_path(boundary)
        mtime = None
        servers = []
        if config_path is not None and config_path.is_file():
            try:
                mtime = config_path.stat().st_mtime
                servers = load_cclsp_config(config_path)
            except (CclspConfigError, OSError) as e:
                raise T.ToolError(
                    f"{config_path} could not be read: {e}. Run "
                    f"`python3 scripts/sync_cclsp.py --write` to regenerate "
                    f"it.") from e
        if not servers:
            searched = ", ".join(str(p)
                                 for p in candidate_config_paths(boundary))
            raise T.ToolError(
                f"No language servers configured for {boundary}. Looked at: "
                f"{searched}. Run `python3 scripts/sync_cclsp.py --write` to "
                f"create one from the servers actually installed.")
        # The engine config is no longer read here: the daemon loads it
        # for the Engine it owns, and a second read could only disagree.
        # The daemon owns the project's one Engine; we attach to it,
        # spawning it if this is the first client. The cclsp config was
        # still read above because a project with no servers must fail
        # here with a message that says so, rather than after a socket
        # round trip.
        # One connection per repository. Opening a second to the same
        # daemon would attach the same session id twice, and the daemon
        # refcounts sessions in a set — so the first detach would
        # release the other entry's locks with no way to tell that
        # apart from the session ending.
        shared = self._connections.get(boundary)
        if shared is not None:
            return _ProjectEngine(root, shared.acquire(), config_path, mtime)

        session = f"lsp-mcp-{os.getpid()}"
        try:
            client = connect_or_spawn(
                project_root=boundary,
                session_id=session,
                # Longer than the hook's 5 s: a first MCP call is
                # interactive and worth waiting out, where a hook that
                # blocks the edit loop is not.
                spawn_wait_s=10.0,
                # The file validated above, so the daemon serves the
                # one this error message would name.
                cclsp_config_path=config_path,
            )
        except (TimeoutError, OSError, RuntimeError) as e:
            raise T.ToolError(
                f"Could not reach the lsp_engine daemon for {root}: {e}. "
                f"The MCP server and the PostToolUse hook share one engine "
                f"per project, so navigation needs that daemon. Check "
                f"`python -m claude_hooks.lsp_engine status --project "
                f"{root}`.") from e
        engine = DaemonEngine(boundary, client)
        # Pinning the config only binds a daemon WE spawn. One that was
        # already running kept whatever it started with, and a daemon
        # started from an environment carrying CCLSP_CONFIG_PATH can
        # legitimately differ from the file resolved here. Saying so is
        # the whole point: the failure this replaces was the MCP
        # validating one file while the daemon served another, with
        # nothing anywhere reporting the difference.
        try:
            served = (client.status() or {}).get("cclsp_config")
            if served and config_path and (
                    Path(served).resolve() != Path(config_path).resolve()):
                engine.add_notice(
                    f"⚠  The lsp_engine daemon for {root} is serving a "
                    f"different cclsp.json than this session validated.\n"
                    f"   validated: {config_path}\n"
                    f"   serving:   {served}\n"
                    f"   It was already running when this session "
                    f"attached, so it kept the file it started with. "
                    f"Reload it to pick up the other one:\n"
                    f"     python -m claude_hooks.lsp_engine reload "
                    f"--project {root}\n"
                    f"   or call the reload_servers tool.")
        except Exception:  # pragma: no cover - never fail the build
            log.debug("could not compare daemon config for %s", root,
                      exc_info=True)
        self._connections[boundary] = engine
        log.info("engine for %s: daemon-backed via %s, %d servers configured",
                 root, boundary, len(servers))
        return _ProjectEngine(root, engine, config_path, mtime)

    def _drop(self, root: Path) -> None:
        entry = self._engines.pop(root, None)
        if entry is not None:
            try:
                entry.engine.shutdown()
            except Exception:  # pragma: no cover — defensive
                log.exception("error shutting down engine for %s", root)
            # The connection is shared across the repository's packages;
            # forget it only when its last holder let go, or the next
            # lookup would hand out a detached client.
            if getattr(entry.engine, "closed", True):
                self._connections.pop(getattr(entry.engine, "root", root),
                                      None)

    def reap_idle(self) -> list[str]:
        """Stop engines nobody has used recently.

        Safe *because* nothing preloads: a reaped engine comes back on
        the next request instead of being gone until the client
        restarts. That ordering is why cclsp could not do this.
        """
        now = time.monotonic()
        dropped: list[str] = []
        with self._lock:
            stale = [r for r, e in self._engines.items()
                     if now - e.last_used > self._idle_seconds]
            for root in stale:
                self._drop(root)
                dropped.append(str(root))
        for root in dropped:
            log.info("reaped idle engine for %s", root)
        return dropped

    def drain_stale_notices(self) -> list[str]:
        """Collect any daemon notices picked up during this call."""
        out: list[str] = []
        with self._lock:
            # By identity: several package entries share one connection,
            # and its notice is once-only — asking it per entry would
            # hand the notice to the first and nothing to the rest,
            # which reads as "only that package's daemon is stale".
            engines = list({id(e.engine): e.engine
                            for e in self._engines.values()}.values())
        for engine in engines:
            try:
                out.extend(engine.take_notices())
            except Exception:  # pragma: no cover - defensive
                continue
        return out

    def running(self) -> list[Path]:
        with self._lock:
            return list(self._engines)

    def shutdown_all(self) -> None:
        with self._lock:
            for root in list(self._engines):
                self._drop(root)


class LspMcpServer:
    """JSON-RPC dispatch surface. Transport-agnostic, like its sibling
    in :mod:`claude_hooks.pgvector_mcp.server`."""

    def __init__(self, registry: Optional[EngineRegistry] = None):
        self.registry = registry or EngineRegistry(
            idle_hours=float(os.environ.get("LSP_MCP_IDLE_HOURS",
                                            DEFAULT_IDLE_HOURS)))

    # ─── JSON-RPC ────────────────────────────────────────────────────

    def handle(self, msg: dict) -> Optional[dict]:
        method = msg.get("method")
        rpc_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            return self._reply(rpc_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self._reply(rpc_id, {"tools": T.tool_catalog()})
        if method == "tools/call":
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            try:
                return self._reply(rpc_id, {
                    "content": [{"type": "text",
                                 "text": self._announce(
                                     self.call_tool(name, args))}],
                    "isError": False,
                })
            except T.ToolError as e:
                # The caller's to fix, so it goes back as prose rather
                # than a stack trace.
                return self._reply(rpc_id, {
                    "content": [{"type": "text",
                                 "text": self._announce(str(e))}],
                    "isError": True,
                })
            except Exception as e:
                log.warning("tool %s failed: %s\n%s", name, e,
                            traceback.format_exc(limit=3))
                return self._reply(rpc_id, {
                    "content": [{"type": "text",
                                 "text": self._announce(
                                     f"{type(e).__name__}: {e}")}],
                    "isError": True,
                })
        if rpc_id is not None:
            return self._reply(rpc_id, error={
                "code": -32601, "message": f"Method not found: {method}"})
        return None

    @staticmethod
    def _reply(rpc_id: Any, result: Optional[dict] = None,
               error: Optional[dict] = None) -> dict:
        out: dict = {"jsonrpc": "2.0", "id": rpc_id}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result or {}
        return out

    # ─── argument helpers ────────────────────────────────────────────

    @staticmethod
    def _file_path(args: dict) -> Path:
        raw = args.get("file_path")
        if not raw or not isinstance(raw, str):
            raise T.ToolError("file_path is required.")
        p = Path(raw)
        if not p.exists():
            raise T.ToolError(
                f"{raw} does not exist. Paths are resolved on the machine "
                f"running this server, not the client.")
        return p

    def _position(self, entry: _ProjectEngine, path: Path,
                  args: dict) -> tuple[int, int]:
        """Resolve line/character, or a symbol name in its place.

        The name form is our addition. It is not a convenience: the
        position-addressed tools are the ones a caller is most likely to
        guess coordinates for, and a guessed position returns a clean
        empty result rather than an error.
        """
        if args.get("line") is not None or args.get("character") is not None:
            return T.to_lsp_position(args.get("line"), args.get("character"))
        name = args.get("symbol_name")
        if not name:
            raise T.ToolError(
                "Provide either line and character (both 1-indexed), or "
                "symbol_name.")
        kind = T_parse_kind(args.get("symbol_kind"))
        res = entry.engine.find_symbols(path, str(name), kind=kind)
        if not res.items:
            raise T.ToolError(_no_symbol_message(name, res))
        if len(res.items) > 1:
            raise T.ToolError(T.render_candidates(res.items, root=entry.root))
        sym = res.items[0]
        if sym.name != name:
            # A substring hit. Saying so costs one line and stops the
            # caller acting on a symbol it did not ask for.
            log.info("resolved %r to %r by substring", name, sym.name)
        return sym.selection.start.line, sym.selection.start.character

    # ─── tools ───────────────────────────────────────────────────────

    def _announce(self, text: str) -> str:
        """Prefix any stale-process notice — ours, the daemon's, or both.

        Applied to every outcome — result, ToolError and unexpected
        exception alike — because a process serving old code is just as
        able to produce the error as the wrong answer, and the caller
        needs the same context either way.
        """
        notices = [n for n in (DETECTOR.banner(),) if n]
        # The daemon holds its own imported code, and it is the one
        # every tool call actually runs on. A stale MCP shim and a stale
        # daemon are different processes with different remedies, so
        # both are reported rather than one standing in for the other.
        try:
            notices.extend(self.registry.drain_stale_notices())
        except Exception:  # pragma: no cover - never fail a tool call
            pass
        return "\n\n".join([*notices, text]) if notices else text

    def call_tool(self, name: str, args: dict) -> str:
        if name not in T.TOOL_NAMES:
            raise T.ToolError(
                f"Unknown tool {name!r}. Available: {', '.join(T.TOOL_NAMES)}")
        handler = getattr(self, f"_tool_{name}")
        return handler(args)

    # -- name-addressed ------------------------------------------------

    def _symbol_positions(self, args: dict) -> tuple[_ProjectEngine, Path, list]:
        path = self._file_path(args)
        entry = self.registry.for_path(path)
        name = args.get("symbol_name")
        if not name:
            raise T.ToolError("symbol_name is required.")
        kind = T_parse_kind(args.get("symbol_kind"))
        res = entry.engine.find_symbols(path, str(name), kind=kind)
        if not res.items:
            raise T.ToolError(_no_symbol_message(name, res))
        return entry, path, res.items

    def _tool_find_definition(self, args: dict) -> str:
        entry, path, syms = self._symbol_positions(args)
        merged = _merge([
            entry.engine.definition(path, s.selection.start.line,
                                    s.selection.start.character)
            for s in syms])
        return T.render_locations(merged, root=entry.root, title="Definitions")

    def _tool_find_references(self, args: dict) -> str:
        entry, path, syms = self._symbol_positions(args)
        include = args.get("include_declaration")
        include = True if include is None else bool(include)
        merged = _merge([
            entry.engine.references(path, s.selection.start.line,
                                    s.selection.start.character,
                                    include_declaration=include)
            for s in syms])
        return T.render_locations(merged, root=entry.root, title="References",
                                  scope=describe_scope(entry.root))

    # -- position-addressed --------------------------------------------

    def _tool_find_implementation(self, args: dict) -> str:
        path = self._file_path(args)
        entry = self.registry.for_path(path)
        line, ch = self._position(entry, path, args)
        return T.render_locations(entry.engine.implementation(path, line, ch),
                                  scope=describe_scope(entry.root),
                                  root=entry.root, title="Implementations")

    def _tool_get_hover(self, args: dict) -> str:
        path = self._file_path(args)
        entry = self.registry.for_path(path)
        line, ch = self._position(entry, path, args)
        return T.render_hover(entry.engine.hover(path, line, ch))

    def _tool_prepare_call_hierarchy(self, args: dict) -> str:
        path = self._file_path(args)
        entry = self.registry.for_path(path)
        line, ch = self._position(entry, path, args)
        res = entry.engine.prepare_call_hierarchy(path, line, ch)
        return T.render_symbols(
            NavResponse(items=[_item_as_symbol(i) for i in res.items],
                        consulted=res.consulted, failures=res.failures,
                        progress=res.progress),
            root=entry.root, title="Call hierarchy items")

    def _tool_get_incoming_calls(self, args: dict) -> str:
        return self._calls(args, "incoming")

    def _tool_get_outgoing_calls(self, args: dict) -> str:
        return self._calls(args, "outgoing")

    def _calls(self, args: dict, direction: str) -> str:
        path = self._file_path(args)
        entry = self.registry.for_path(path)
        line, ch = self._position(entry, path, args)
        return T.render_calls(
            entry.engine.calls(path, line, ch, direction=direction),
            root=entry.root, direction=direction)

    # -- whole-file / whole-project ------------------------------------

    def _tool_get_diagnostics(self, args: dict) -> str:
        path = self._file_path(args)
        entry = self.registry.for_path(path)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise T.ToolError(f"cannot read {path}: {e}") from e
        if not entry.engine.did_open(path, content):
            return ("NOT ANALYSED — no configured language server claims "
                    f"{path.suffix or 'this file type'}. This is not a "
                    "statement about the code.")
        res = entry.engine.get_diagnostics_result(path)
        diags = res.items
        if not diags:
            if not res.settled:
                # The failure this whole tool exists to avoid: clangd
                # needs AST + clang-tidy before its first push on a
                # large TU, and reporting that wait as "no diagnostics"
                # is a clean bill of health the server never gave.
                who = res.server or "the language server"
                return (
                    f"NO ANSWER YET — {who} did not publish diagnostics for "
                    f"{path.name} within {res.timeout:.0f}s. This is not a "
                    f"statement about the code: a cold translation unit "
                    f"(large preamble, background index) can legitimately "
                    f"take longer. Re-run once it has warmed up, or raise "
                    f"`diagnosticsTimeout` for that server in cclsp.json.")
            return f"No diagnostics for {path.name}."
        header = f"Diagnostics for {path.name} ({len(diags)})"
        if not res.settled:
            # A file claimed by two servers where only one replied: the
            # list is real but it is not the whole file, and an
            # unqualified count reads as though it were.
            header += (f" — PARTIAL, {res.server} did not answer within "
                       f"{res.timeout:.0f}s")
        lines = [header + ":"]
        for d in diags:
            sev = {1: "error", 2: "warning", 3: "info",
                   4: "hint"}.get(getattr(d, "severity", 0), "note")
            lines.append(f"  {sev} {d.line + 1}:{d.character + 1} "
                         f"{d.message}")
        return "\n".join(lines)

    def _tool_find_workspace_symbols(self, args: dict) -> str:
        query = args.get("query")
        if not query:
            raise T.ToolError("query is required.")
        hint = args.get("file_path")
        entry = self._project_hint(hint)
        res = entry.engine.workspace_symbols(
            str(query), start_all=bool(args.get("start_all")))
        return T.render_symbols(res, root=entry.root,
                                title=f"Workspace symbols for {query!r}")

    def _tool_reload_servers(self, args: dict) -> str:
        entry = self._project_hint(args.get("file_path"))
        keep = bool(args.get("keep_config"))
        try:
            result = entry.engine.reload(config=not keep)
        except AttributeError:
            raise T.ToolError(
                "This project is served by an in-process engine, which has "
                "no configuration to reload. Use restart_server instead.")
        except RuntimeError as e:
            if "unknown op" not in str(e):
                raise
            raise T.ToolError(
                f"The lsp_engine daemon for {entry.root} is running code "
                f"that predates this operation — it is a long-lived process "
                f"and restarting this MCP client does not restart it. "
                f"Restart it with:\n"
                f"  python -m claude_hooks.lsp_engine restart --project "
                f"{entry.root}")
        stopped = result.get("stopped") or []
        # Name the repository, not the package that was passed in. One
        # daemon serves the whole tree, so this stops every package's
        # servers — saying "reloaded <package>" would understate what
        # just happened to a colleague's warm engine next door.
        scope = getattr(entry.engine, "root", entry.root)
        lines = [
            f"Reloaded the lsp_engine daemon for {scope}.",
            f"Stopped {len(stopped)} package engine(s) and their language "
            f"servers; they start again on the next request.",
        ]
        if result.get("reloaded_config"):
            lines.append(f"Re-read {result.get('cclsp_config')}.")
        else:
            lines.append("Kept the configuration already loaded.")
        for root in stopped:
            lines.append(f"  - {root}")
        return "\n".join(lines)

    def _tool_restart_server(self, args: dict) -> str:
        hint = args.get("file_path")
        exts = args.get("extensions")
        if hint is None and not self.registry.running():
            return "No language servers are running; nothing to restart."
        entry = self._project_hint(hint)
        stopped = entry.engine.restart(
            [str(e) for e in exts] if isinstance(exts, list) else None)
        if not stopped:
            scope = f" for {', '.join(exts)}" if exts else ""
            return (f"No running servers matched{scope} in {entry.root}. "
                    f"Nothing was restarted.")
        return (f"Restarted {len(stopped)} server(s) in {entry.root}: "
                f"{', '.join(stopped)}. They will start again on the next "
                f"request.")

    def _project_hint(self, hint) -> _ProjectEngine:
        """Pick the project for a tool that has no file to route on."""
        if hint:
            return self.registry.for_path(str(hint))
        running = self.registry.running()
        if len(running) == 1:
            return self.registry.for_path(running[0])
        if not running:
            return self.registry.for_path(Path.cwd())
        raise T.ToolError(
            "Several projects are open ("
            + ", ".join(str(r) for r in running)
            + "). Pass file_path to say which one you mean.")

    # -- rename --------------------------------------------------------

    def _tool_rename_symbol(self, args: dict) -> str:
        path = self._file_path(args)
        entry = self.registry.for_path(path)
        new_name = _require_new_name(args)
        name = args.get("symbol_name")
        if not name:
            raise T.ToolError("symbol_name is required.")
        kind = T_parse_kind(args.get("symbol_kind"))
        found = entry.engine.find_symbols(path, str(name), kind=kind)
        if not found.items:
            raise T.ToolError(_no_symbol_message(name, found))
        if len(found.items) > 1:
            return T.render_candidates(found.items, root=entry.root)
        sym = found.items[0]
        return self._do_rename(entry, path, sym.selection.start.line,
                               sym.selection.start.character, new_name, args)

    def _tool_rename_symbol_strict(self, args: dict) -> str:
        path = self._file_path(args)
        entry = self.registry.for_path(path)
        new_name = _require_new_name(args)
        line, ch = T.to_lsp_position(args.get("line"), args.get("character"))
        return self._do_rename(entry, path, line, ch, new_name, args)

    def _do_rename(self, entry: _ProjectEngine, path: Path, line: int,
                   ch: int, new_name: str, args: dict) -> str:
        res = entry.engine.rename(path, line, ch, new_name)
        if not res.items:
            note = T.provenance_note(res)
            return ("No rename edits were produced — the server declined or "
                    "the position is not on a renameable symbol."
                    + (f"\n\n{note}" if note else ""))
        edit = res.items[0]
        apply = _wants_apply(args)
        if apply:
            written = _apply_edit(edit)
            return T.render_rename(edit, root=entry.root, applied=True,
                                   new_name=new_name) + \
                f"\n\nWrote {written} file(s)."
        return T.render_rename(edit, root=entry.root, applied=False,
                               new_name=new_name)


# ─────────────────────────── helpers ────────────────────────────────


def T_parse_kind(value) -> Optional[int]:
    from claude_hooks.lsp_engine.protocol import parse_symbol_kind
    if value in (None, ""):
        return None
    kind = parse_symbol_kind(value)
    if kind is None:
        raise T.ToolError(
            f"Unknown symbol_kind {value!r}. Use one of: function, class, "
            f"method, variable, constant, interface, struct, enum, field, "
            f"property, namespace, module — or omit it.")
    return kind


def _no_symbol_message(name, res: NavResponse) -> str:
    note = T.provenance_note(res)
    base = f"No symbol named {name!r} found in that file."
    if note:
        # Without this the caller reads a server problem as "the symbol
        # is not there", and goes looking for a name that exists.
        return f"{base}\n\n{note}"
    return base + (" The name must match exactly (no prefix matching).")


def _merge(responses: list[NavResponse]) -> NavResponse:
    """Combine per-symbol responses, keeping every provenance field.

    ``find_definition`` on an overloaded name queries once per match;
    losing the failures from all but the last would report a partial
    answer as complete.
    """
    items: list = []
    consulted: list[str] = []
    failures: list = []
    progress = None
    truncated = 0
    for r in responses:
        items.extend(r.items)
        for c in r.consulted:
            if c not in consulted:
                consulted.append(c)
        failures.extend(r.failures)
        progress = progress or r.progress
        truncated = max(truncated, r.scan_truncated_at)
    return NavResponse(items=items, consulted=tuple(consulted),
                       failures=tuple(failures), progress=progress,
                       scan_truncated_at=truncated)


def _item_as_symbol(item):
    from claude_hooks.lsp_engine.protocol import Symbol
    return Symbol(name=item.name, kind=item.kind, uri=item.uri,
                  range=item.range, selection=item.selection,
                  detail=item.detail)


def _require_new_name(args: dict) -> str:
    new_name = args.get("new_name")
    if not new_name or not isinstance(new_name, str):
        raise T.ToolError("new_name is required.")
    return new_name


def _wants_apply(args: dict) -> bool:
    """Preview unless explicitly told to write.

    This inverts cclsp's default, which applied unless ``dry_run`` was
    passed. A rename can touch dozens of files across a workspace, and
    an exploratory call should not rewrite them. ``dry_run`` is still
    honoured so existing callers behave sensibly: the failure mode of
    this inversion is a rename that did not happen and said so, which is
    recoverable, against one that happened and was not expected, which
    is not.
    """
    if args.get("apply") is not None:
        return bool(args["apply"])
    if args.get("dry_run") is not None:
        return not bool(args["dry_run"])
    return False


def _apply_edit(edit) -> int:
    """Write a :class:`WorkspaceEdit` to disk.

    Edits are applied **bottom-up per file** so earlier ranges stay
    valid: rewriting line 3 first shifts every offset below it, and the
    remaining edits then land in the wrong place — corrupting the file
    while reporting success.
    """
    by_file: dict[str, list] = {}
    for e in edit.edits:
        by_file.setdefault(T.uri_to_path(e.uri), []).append(e)

    written = 0
    for path, edits in by_file.items():
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            raise T.ToolError(f"cannot read {path} to apply the rename: {e}")
        lines = text.splitlines(keepends=True)
        for e in sorted(edits, key=lambda x: (x.range.start.line,
                                              x.range.start.character),
                        reverse=True):
            lines = _splice(lines, e)
        p.write_text("".join(lines), encoding="utf-8")
        written += 1
    return written


def _splice(lines: list[str], edit) -> list[str]:
    sl, sc = edit.range.start.line, edit.range.start.character
    el, ec = edit.range.end.line, edit.range.end.character
    if sl >= len(lines):
        return lines
    el = min(el, len(lines) - 1)
    head = lines[sl][:sc]
    tail = lines[el][ec:]
    return lines[:sl] + [head + edit.new_text + tail] + lines[el + 1:]


# ─────────────────────────── transport ──────────────────────────────


def serve_stdio(registry: Optional[EngineRegistry] = None) -> int:
    """Run the JSON-RPC loop until stdin closes.

    **EOF is the shutdown signal.** cclsp ignored it, which is why six
    orphaned servers were still holding memory on solidpc eighty days
    after the sessions that started them had gone. A stdio server whose
    client has closed has no way to be reached and no reason to live.
    """
    force_utf8_stdio()
    server = LspMcpServer(registry)
    stop = threading.Event()

    def reaper():
        while not stop.wait(3600.0):
            try:
                server.registry.reap_idle()
            except Exception:  # pragma: no cover — defensive
                log.exception("idle reap failed")

    t = threading.Thread(target=reaper, name="lsp-mcp-reaper", daemon=True)
    t.start()

    sys.stderr.write(f"{SERVER_NAME} {SERVER_VERSION} on stdio "
                     f"(protocol={PROTOCOL_VERSION})\n")
    sys.stderr.flush()
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as e:
                log.warning("malformed JSON: %s", e)
                continue
            resp = server.handle(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
    finally:
        stop.set()
        server.registry.shutdown_all()
    return 0
