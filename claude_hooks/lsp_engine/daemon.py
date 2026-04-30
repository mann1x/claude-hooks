"""LSP engine daemon — one process per project.

Hosts an ``Engine`` (multi-LSP routing), a ``SessionLockManager``
(per-file affinity locks per Decision 5), and an ``IpcServer``
(UNIX socket the hook talks to). One sweeper thread drains expired
locks every second so even a quiet system eventually transitions
ownership when a session goes idle.

Project keying: the daemon's working directory uniquely identifies
it. Two Claude Code sessions in the same absolute project root
share one daemon; sessions in different roots get their own.
``project_dir(root)`` returns ``~/.claude/lsp-engine/<sha256-prefix>/``
for a given root.

Lifecycle:

- ``Daemon.run()`` blocks the caller. Used by the spawned-detached
  process; tests call ``Daemon.start()`` and ``Daemon.stop()``
  instead so they can assert state.
- The lock file (``daemon.lock``) carries the PID. ``flock(LOCK_EX
  | LOCK_NB)`` is held for the daemon's lifetime so a second
  ``Daemon.run()`` for the same project fails fast instead of
  fighting the running one for the socket.
- Shutdown reaps the engine, closes the socket, removes the lock
  and socket files. SIGTERM and SIGINT trigger graceful shutdown.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Optional

from claude_hooks.lsp_engine.config import (
    EngineConfig,
    LspServerSpec,
    load_cclsp_config,
    load_engine_config,
)
from claude_hooks.lsp_engine.engine import Engine
from claude_hooks.lsp_engine.git_watch import GitWatcher
from claude_hooks.lsp_engine.ipc import IpcServer
from claude_hooks.lsp_engine.locks import (
    QueuedChange,
    SessionLockManager,
)
from claude_hooks.lsp_engine.preload import preload_engine

log = logging.getLogger("claude_hooks.lsp_engine.daemon")


SWEEPER_INTERVAL_S = 1.0
DEFAULT_DIAG_TIMEOUT_S = 2.0


def project_dir(project_root: str | os.PathLike, base: Optional[Path] = None) -> Path:
    """Return the per-project state directory for ``project_root``.

    Hashing the absolute path keeps the directory name short and
    avoids leaking the full project path through process listings;
    we still write the resolved path inside the dir as ``project``
    for human inspection.
    """
    abs_root = Path(project_root).resolve()
    digest = hashlib.sha256(str(abs_root).encode("utf-8")).hexdigest()[:16]
    base = base or (Path.home() / ".claude" / "lsp-engine")
    return base / digest


def socket_path_for(project_root: str | os.PathLike, base: Optional[Path] = None) -> Path:
    return project_dir(project_root, base=base) / "daemon.sock"


def lock_path_for(project_root: str | os.PathLike, base: Optional[Path] = None) -> Path:
    return project_dir(project_root, base=base) / "daemon.lock"


class DaemonAlreadyRunning(RuntimeError):
    """Raised when ``Daemon.start()`` finds another live daemon for
    the same project (lock file held by a live process)."""


class Daemon:
    """One project, one daemon. Threading-server, sweeper thread,
    lock file held for life.
    """

    def __init__(
        self,
        project_root: str | os.PathLike,
        servers: list[LspServerSpec],
        engine_config: Optional[EngineConfig] = None,
        *,
        state_base: Optional[Path] = None,
        startup_timeout: float = 10.0,
        request_timeout: float = 5.0,
        git_poll_interval: float = 1.0,
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._dir = project_dir(self._project_root, base=state_base)
        self._socket_path = self._dir / "daemon.sock"
        self._lock_path = self._dir / "daemon.lock"
        self._project_hint_path = self._dir / "project"

        cfg = engine_config or EngineConfig()
        self._lock_manager = SessionLockManager(
            debounce_seconds=cfg.session_locks.debounce_seconds,
        )
        self._engine = Engine(
            project_root=self._project_root,
            servers=servers,
            config=cfg,
            startup_timeout=startup_timeout,
            request_timeout=request_timeout,
        )
        self._engine_config = cfg

        self._ipc = IpcServer(
            self._socket_path,
            handler=self._handle_request,
            on_disconnect=self._on_connection_close,
        )

        # Map connection-thread-id -> set of session_ids attached on
        # that connection. Used to release locks when a connection
        # drops without an explicit detach (crashed Claude Code, etc).
        self._sessions_by_thread: dict[int, set[str]] = {}
        self._sessions_lock = threading.Lock()
        # Refcount of attached sessions for ``status`` and tests.
        self._attached_sessions: set[str] = set()

        self._sweeper_thread: Optional[threading.Thread] = None
        self._sweeper_stop = threading.Event()
        self._lock_fd: Optional[int] = None
        self._stopping = threading.Event()

        # Phase 2 additions: adaptive preload + git watcher.
        self._preload_thread: Optional[threading.Thread] = None
        self._git_watcher: Optional[GitWatcher] = None
        self._git_poll_interval = float(git_poll_interval)
        self._refresh_lock = threading.Lock()

    # ─── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._project_hint_path.write_text(
            str(self._project_root) + "\n", encoding="utf-8",
        )
        self._acquire_lock_file()
        try:
            self._ipc.start_in_background()
        except Exception:
            self._release_lock_file()
            raise
        self._sweeper_thread = threading.Thread(
            target=self._sweeper_loop,
            name="lsp-engine-sweeper",
            daemon=True,
        )
        self._sweeper_thread.start()

        # Adaptive preload (off the critical path — runs while
        # sessions connect). Soft-fails if graph.json is missing.
        if self._engine_config.preload.use_code_graph:
            self._preload_thread = threading.Thread(
                target=self._run_preload,
                name="lsp-engine-preload",
                daemon=True,
            )
            self._preload_thread.start()

        # Git watcher fires the bulk-refresh on branch switches /
        # pulls / resets. Inactive for non-git projects.
        self._git_watcher = GitWatcher(
            self._project_root,
            on_change=self._on_git_change,
            poll_interval=self._git_poll_interval,
        )
        self._git_watcher.start()

        log.info("lsp-engine daemon started for %s", self._project_root)

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._sweeper_stop.set()
        if self._git_watcher is not None:
            try:
                self._git_watcher.stop()
            except Exception:  # pragma: no cover — defensive
                log.exception("error stopping git watcher")
            self._git_watcher = None
        try:
            self._ipc.shutdown()
        except Exception:  # pragma: no cover — defensive
            log.exception("error shutting down ipc")
        if self._sweeper_thread is not None:
            self._sweeper_thread.join(timeout=2.0)
            self._sweeper_thread = None
        if self._preload_thread is not None:
            # Preload thread is daemon=True so it dies with the
            # process anyway; join briefly so a fast stop() doesn't
            # leave a thread mid-LSP-call.
            self._preload_thread.join(timeout=2.0)
            self._preload_thread = None
        try:
            self._engine.shutdown()
        except Exception:  # pragma: no cover — defensive
            log.exception("error shutting down engine")
        self._release_lock_file()
        log.info("lsp-engine daemon stopped for %s", self._project_root)

    def run(self) -> None:
        """Blocking entry point used by ``__main__``. Installs SIGTERM
        / SIGINT handlers and waits for shutdown. Daemonisation
        (double-fork, setsid) is the spawner's job — we just run.
        """
        signal.signal(signal.SIGTERM, lambda *_: self._stopping.set())
        signal.signal(signal.SIGINT, lambda *_: self._stopping.set())
        self.start()
        try:
            while not self._stopping.wait(timeout=0.5):
                pass
        finally:
            self.stop()

    # ─── lock file (POSIX flock) ─────────────────────────────────────

    def _acquire_lock_file(self) -> None:
        fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise DaemonAlreadyRunning(
                    f"another daemon already holds {self._lock_path}",
                ) from e
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n{int(time.time())}\n".encode("ascii"))
        os.fsync(fd)
        self._lock_fd = fd

    def _release_lock_file(self) -> None:
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover — defensive
            pass
        try:
            os.close(self._lock_fd)
        except OSError:  # pragma: no cover — defensive
            pass
        self._lock_fd = None
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass

    # ─── sweeper ─────────────────────────────────────────────────────

    def _sweeper_loop(self) -> None:
        while not self._sweeper_stop.wait(timeout=SWEEPER_INTERVAL_S):
            drained = self._lock_manager.tick()
            self._apply_drained(drained)

    # ─── preload + git refresh ───────────────────────────────────────

    def _run_preload(self) -> None:
        """Background thread entry point for adaptive preload.

        Caps preload to extensions any configured server actually
        claims so we don't read 200 ``.md`` files for a Python-only
        daemon. ``preload_engine`` soft-fails on missing graph.json.
        """
        try:
            cfg = self._engine_config.preload
            extensions = self._engine.configured_extensions()
            preload_engine(
                self._engine,
                self._project_root,
                max_files=cfg.max_files,
                extension_filter=extensions if extensions else None,
            )
        except Exception:  # pragma: no cover — defensive
            log.exception("preload thread crashed")

    def _on_git_change(self, reason: str) -> None:
        """Called from the git-watcher thread on HEAD / ref changes.

        Bulk-refreshes every currently-open file from disk so the LSP
        sees the new branch's content. We *don't* clear lock state —
        sessions with queued changes will drain naturally; if their
        queued content is stale relative to the new branch, that's a
        user-visible "I edited on the wrong branch" issue rather than
        something the engine can paper over.
        """
        with self._refresh_lock:
            log.info("git_watch: %s — refreshing open files", reason)
            try:
                refreshed = self._engine.refresh_open_files()
            except Exception:  # pragma: no cover — defensive
                log.exception("git_watch: refresh_open_files raised")
                return
            log.info("git_watch: refreshed %d open files", refreshed)

    def _apply_drained(self, drained: list[tuple[str, QueuedChange]]) -> None:
        """For each (path, queued_change) returned by the manager,
        forward the queued content to the LSP. The lock has already
        been transferred to the queued session, so we just push the
        content through.
        """
        for path, change in drained:
            try:
                self._engine.did_change(path, change.content)
            except Exception:  # pragma: no cover — defensive
                log.exception("failed to apply drained change for %s", path)

    # ─── connection lifecycle ────────────────────────────────────────

    def _on_connection_close(self) -> None:
        """Called by the IPC layer when a client disconnects. Release
        any sessions still associated with this thread.
        """
        tid = threading.get_ident()
        with self._sessions_lock:
            sessions = self._sessions_by_thread.pop(tid, set())
        for sid in sessions:
            self._detach_session(sid)

    def _attach_session(self, session_id: str) -> None:
        tid = threading.get_ident()
        with self._sessions_lock:
            self._sessions_by_thread.setdefault(tid, set()).add(session_id)
            self._attached_sessions.add(session_id)

    def _detach_session(self, session_id: str) -> None:
        tid = threading.get_ident()
        with self._sessions_lock:
            self._sessions_by_thread.get(tid, set()).discard(session_id)
            self._attached_sessions.discard(session_id)
        drained = self._lock_manager.release_session(session_id)
        self._apply_drained(drained)

    # ─── request dispatch ────────────────────────────────────────────

    def _handle_request(self, req: dict) -> dict:
        rid = req.get("id")
        op = req.get("op")
        session = req.get("session")
        if not isinstance(op, str):
            return {"id": rid, "ok": False, "error": "missing 'op'"}
        if not isinstance(session, str) or not session:
            return {"id": rid, "ok": False, "error": "missing 'session'"}

        try:
            if op == "attach":
                self._attach_session(session)
                return {"id": rid, "ok": True}
            if op == "detach":
                self._detach_session(session)
                return {"id": rid, "ok": True}
            if op == "did_open":
                return self._op_did_open(rid, session, req)
            if op == "did_change":
                return self._op_did_change(rid, session, req)
            if op == "did_close":
                return self._op_did_close(rid, session, req)
            if op == "diagnostics":
                return self._op_diagnostics(rid, session, req)
            if op == "status":
                return self._op_status(rid)
            if op == "shutdown":
                # Graceful shutdown for tests / ops. Run in a thread
                # so the response goes back before we tear down.
                threading.Thread(target=self.stop, daemon=True).start()
                return {"id": rid, "ok": True}
            return {"id": rid, "ok": False, "error": f"unknown op: {op!r}"}
        except Exception as e:  # pragma: no cover — defensive
            log.exception("op %s crashed", op)
            return {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}

    def _op_did_open(self, rid, session, req: dict) -> dict:
        path = req.get("path")
        content = req.get("content")
        if not isinstance(path, str) or content is None:
            return {"id": rid, "ok": False, "error": "did_open requires path + content"}
        # did_open *always* takes the lock for the calling session —
        # opening a file is "I'm working on this now". No queuing.
        forward, drained = self._lock_manager.did_change(session, path, content)
        self._apply_drained(drained)
        opened = False
        if forward:
            opened = self._engine.did_open(path, content)
        return {"id": rid, "ok": True, "opened": opened}

    def _op_did_change(self, rid, session, req: dict) -> dict:
        path = req.get("path")
        content = req.get("content")
        if not isinstance(path, str) or content is None:
            return {"id": rid, "ok": False, "error": "did_change requires path + content"}
        forward, drained = self._lock_manager.did_change(session, path, content)
        self._apply_drained(drained)
        if forward:
            forwarded = self._engine.did_change(path, content)
            if not forwarded:
                # File was never opened — fall back to did_open so
                # the LSP sees something. Common when a session
                # attaches mid-edit on a file from disk.
                self._engine.did_open(path, content)
                forwarded = True
            return {"id": rid, "ok": True, "forwarded": True, "queued_behind": None}
        owner = self._lock_manager.owner_of(path)
        return {
            "id": rid,
            "ok": True,
            "forwarded": False,
            "queued_behind": owner,
        }

    def _op_did_close(self, rid, session, req: dict) -> dict:
        path = req.get("path")
        if not isinstance(path, str):
            return {"id": rid, "ok": False, "error": "did_close requires path"}
        closed = self._engine.did_close(path)
        return {"id": rid, "ok": True, "closed": closed}

    def _op_diagnostics(self, rid, session, req: dict) -> dict:
        path = req.get("path")
        if not isinstance(path, str):
            return {"id": rid, "ok": False, "error": "diagnostics requires path"}
        timeout_ms = int(req.get("timeout_ms")
                         or self._engine_config.session_locks.query_timeout_ms)
        diag_timeout_s = float(req.get("diag_timeout_s") or DEFAULT_DIAG_TIMEOUT_S)

        can_forward, drained = self._lock_manager.query(
            session, path, timeout_ms=timeout_ms,
        )
        self._apply_drained(drained)
        stale = not can_forward  # we forward anyway per Decision 5
        diags = self._engine.get_diagnostics(path, timeout=diag_timeout_s)
        return {
            "id": rid,
            "ok": True,
            "diagnostics": [_diag_to_json(d) for d in diags],
            "stale": stale,
        }

    def _op_status(self, rid) -> dict:
        with self._sessions_lock:
            sessions = list(self._attached_sessions)
        return {
            "id": rid,
            "ok": True,
            "project": str(self._project_root),
            "sessions": sessions,
            "open_files": self._engine.open_files(),
            "active_servers": [
                spec.command[0] for spec in self._engine.active_servers()
            ],
            "held_uris": self._lock_manager.held_uris(),
        }


def _diag_to_json(d) -> dict:
    return {
        "uri": d.uri,
        "severity": d.severity,
        "line": d.line,
        "character": d.character,
        "message": d.message,
        "code": d.code,
        "source": d.source,
    }


# ─── helpers for the spawn flow ──────────────────────────────────────


def load_daemon_config(
    project_root: str | os.PathLike,
    *,
    cclsp_config_path: Optional[str | os.PathLike] = None,
) -> tuple[list[LspServerSpec], EngineConfig]:
    """Resolve the cclsp.json + lsp-engine.toml for ``project_root``.

    cclsp.json is found via the env var the user already sets for
    cclsp itself (``CCLSP_CONFIG_PATH``) or, failing that, falls
    back to ``<project_root>/cclsp.json``. lsp-engine.toml lives at
    ``<project_root>/.claude-hooks/lsp-engine.toml`` by convention.
    Both are optional; an empty servers list yields a daemon that
    starts but answers nothing useful.
    """
    root = Path(project_root).resolve()
    cclsp_path = (
        Path(cclsp_config_path)
        if cclsp_config_path
        else Path(os.environ.get("CCLSP_CONFIG_PATH", str(root / "cclsp.json")))
    )
    servers = load_cclsp_config(cclsp_path)
    engine_cfg = load_engine_config(
        project_path=root / ".claude-hooks" / "lsp-engine.toml",
    )
    return servers, engine_cfg
