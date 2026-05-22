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
import hashlib
import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Optional

# fcntl exists on POSIX only; msvcrt.locking is the Windows equivalent.
# Both are stdlib. We import lazily-by-platform so neither import
# failure breaks the cross-platform module load.
if os.name == "nt":
    import msvcrt  # type: ignore[import-not-found]
    fcntl = None  # sentinel; daemon._acquire_lock_file picks the right path
else:
    import fcntl  # type: ignore[no-redef]
    msvcrt = None  # sentinel

from claude_hooks.lsp_engine.compile import CompileOrchestrator
from claude_hooks.lsp_engine.config import (
    EngineConfig,
    LspServerSpec,
    load_cclsp_config,
    load_engine_config,
)
from claude_hooks.lsp_engine.engine import Engine
from claude_hooks.lsp_engine.git_watch import GitWatcher
from claude_hooks.lsp_engine.ipc import IpcServer, windows_pipe_name_for
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

    v1.10.3: case-normalize before hashing via :func:`os.path.normcase`.
    On Windows that lowercases the drive letter and switches separators
    so semantically-identical paths from different callers
    (``c:\\Users\\…`` from the hook, ``C:\\Users\\…`` from a manual
    ``--project`` flag) produce the same digest. Without this, the hook
    and CLI never meet — different state dir, different socket, hook
    sees ``daemon socket did not come up in time`` indefinitely
    (pandorum 2026-05-22). ``os.path.normcase`` is a no-op on POSIX so
    Linux behaviour is unchanged. The same normalization must be
    applied in :func:`claude_hooks.lsp_engine.ipc.windows_pipe_name_for`
    to keep the pipe name consistent.
    """
    abs_root = Path(project_root).resolve()
    key = os.path.normcase(str(abs_root))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    base = base or (Path.home() / ".claude" / "lsp-engine")
    return base / digest


def socket_path_for(project_root: str | os.PathLike, base: Optional[Path] = None):
    """Return the IPC address for ``project_root``.

    POSIX: a ``Path`` to ``daemon.sock`` inside the per-project state
    directory.
    Windows: a ``str`` named pipe like ``\\\\.\\pipe\\claude-hooks-lsp-engine-<hash>``
    (no filesystem entry — pipes live in the kernel's pipe namespace).
    """
    if os.name == "nt":
        return windows_pipe_name_for(project_root)
    return project_dir(project_root, base=base) / "daemon.sock"


def lock_path_for(project_root: str | os.PathLike, base: Optional[Path] = None) -> Path:
    return project_dir(project_root, base=base) / "daemon.lock"


def pid_is_alive(pid: int) -> bool:
    """Best-effort cross-platform liveness probe for ``pid``.

    Returns True if a process with that PID currently exists, False if
    not (or if the probe can't determine). Used by the ``status`` CLI
    to distinguish a live daemon from a stale lock file pointing at a
    PID that's been reaped, and by ``cleanup`` to know when it's safe
    to remove the per-project state dir.

    POSIX: ``os.kill(pid, 0)`` raises ``ProcessLookupError`` when the
    PID doesn't exist, ``PermissionError`` when it exists but we don't
    own it (still alive — treated as True). Other ``OSError`` ⇒ False.

    Windows: try ``ctypes.windll.kernel32.OpenProcess`` with
    ``PROCESS_QUERY_LIMITED_INFORMATION`` (0x1000). Non-null handle ⇒
    process exists; close it and return True. Null handle ⇒ either no
    such PID or access denied — we can distinguish via
    ``GetLastError`` (5 = access denied = alive; 87 = invalid param =
    dead). Failure to load ctypes ⇒ False (conservative).

    PID 0 / negative ⇒ False (no legitimate daemon ever sits there).
    """
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes  # local import: only on Windows
            from ctypes import wintypes
        except ImportError:  # pragma: no cover — ctypes ships with CPython
            return False
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # NULL handle: distinguish "access denied" (5) from "no such
        # PID" (87 — ERROR_INVALID_PARAMETER for OpenProcess).
        err = kernel32.GetLastError()
        # 5 = ERROR_ACCESS_DENIED → process exists, we just can't open it.
        return err == 5
    # POSIX
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else.
        return True
    except OSError:
        return False
    return True


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
        # IPC address: filesystem socket path on POSIX, ``\\.\pipe\<name>``
        # on Windows. Going through ``socket_path_for`` is load-bearing —
        # building it manually as ``self._dir / "daemon.sock"`` produces a
        # filesystem path that Windows rejects as a named-pipe name
        # (``WinError 123``), which silently broke the LSP engine on
        # Windows from v1.9.0 → v1.9.4. The ``daemon.lock`` and
        # ``project`` hint files still live on disk under ``self._dir``;
        # only the live IPC endpoint switches namespace per platform.
        self._socket_path = socket_path_for(self._project_root, base=state_base)
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

        # Phase 3: opt-in compile-aware orchestrator. Only constructed
        # when the user has explicitly turned it on in config — the
        # default-off invariant is enforced here, not at the runner
        # layer (so misconfigured users see "compile_aware: false"
        # behaviour, not a half-active orchestrator).
        self._compile: Optional[CompileOrchestrator] = None
        if cfg.compile_aware.enabled and cfg.compile_aware.commands:
            # Pass the toml path so the orchestrator can hot-reload
            # commands on edit. By convention the daemon looks at
            # ``<project-root>/.claude-hooks/lsp-engine.toml`` (see
            # :func:`load_daemon_config`). Pre-v1.10.4 the daemon
            # held its commands as a frozen snapshot from startup and
            # never re-read the toml; the user had to kill the
            # daemon to apply edits — and on Windows there was no
            # supported way to do that. Hot-reload closes that gap.
            self._compile = CompileOrchestrator.from_engine_config(
                self._project_root,
                cfg.compile_aware.commands,
                toml_path=self._project_root / ".claude-hooks" / "lsp-engine.toml",
            )

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

        if self._compile is not None:
            self._compile.start()

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
        if self._compile is not None:
            try:
                self._compile.stop()
            except Exception:  # pragma: no cover — defensive
                log.exception("error stopping compile orchestrator")
            self._compile = None
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

    # ─── lock file (POSIX flock / Windows msvcrt.locking) ────────────

    # Byte offset of the Windows exclusion lock — well past any
    # plausible PID + timestamp payload so other processes can
    # ``read_text()`` the lock file without hitting the lock. See
    # :func:`_acquire_lock_file` for the gory rationale.
    _WIN_LOCK_OFFSET = 4096

    def _acquire_lock_file(self) -> None:
        fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                # ``msvcrt.locking`` (LK_NBLCK) is exclusive at the
                # byte-range level — ANY other process trying to
                # read the locked bytes hits ERROR_LOCK_VIOLATION
                # ("Device or resource busy"). Pre-v1.10.6 we locked
                # byte 0, exactly where the PID is written, so the
                # ``daemon_pid()`` reader (and any operator running
                # ``type daemon.lock``) saw EBUSY. The fix: lock a
                # byte FAR past the payload — Windows lets you lock
                # bytes beyond EOF (the lock is a reservation, no
                # underlying file growth required). Other processes
                # reading bytes 0..N where N << ``_WIN_LOCK_OFFSET``
                # don't conflict with the lock at byte 4096.
                #
                # This obsoletes the v1.10.4 workaround of stuffing
                # ``os.getpid()`` into the daemon's IPC ``status``
                # response — we keep that workaround for backwards
                # compatibility (downstream consumers may rely on
                # it), but the lock file is now the authoritative
                # PID source again.
                os.lseek(fd, self._WIN_LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[union-attr]
                os.lseek(fd, 0, os.SEEK_SET)  # reset for the upcoming write
            else:
                # POSIX ``flock`` is whole-file advisory and doesn't
                # block reads — ``daemon_pid()`` works without
                # special handling.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[union-attr]
        except OSError as e:
            os.close(fd)
            # Windows raises ``OSError`` with errno=EACCES when the
            # range is already locked; POSIX raises EAGAIN/EWOULDBLOCK.
            if e.errno in (
                errno.EWOULDBLOCK,
                errno.EAGAIN,
                errno.EACCES,
                errno.EDEADLK,
            ):
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
            if os.name == "nt":
                # Mirror the acquire-time offset: unlock the same
                # byte we locked. Pre-v1.10.6 we unlocked at 0;
                # that's the wrong byte now and would leak the
                # lock until process exit (CRT cleans up on close,
                # so user-visible behaviour was fine, but explicit
                # unlock is hygienic).
                os.lseek(self._lock_fd, self._WIN_LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(self._lock_fd, msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
            else:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
        except OSError:  # pragma: no cover — defensive
            pass
        try:
            os.close(self._lock_fd)
        except OSError:  # pragma: no cover — defensive
            pass
        self._lock_fd = None
        try:
            self._lock_path.unlink()
        except (FileNotFoundError, PermissionError):
            # Windows refuses to unlink a file held open by *any*
            # process; the lock-fd close above usually solves it but
            # leftover handles from a half-shutdown can keep it
            # around. Leaving the file is harmless — next start
            # truncates it.
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
            if opened and self._compile is not None:
                self._compile.notify_change(path)
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
            if self._compile is not None:
                self._compile.notify_change(path)
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
        # Merge compile-aware diagnostics on top, distinguished by
        # ``Diagnostic.source`` so the consumer can filter (cargo /
        # tsc / mypy show up as their tool name; the LSP entries
        # carry pyright / rust-analyzer / etc).
        if self._compile is not None:
            diags = list(diags) + self._compile.get_diagnostics(path)
        return {
            "id": rid,
            "ok": True,
            "diagnostics": [_diag_to_json(d) for d in diags],
            "stale": stale,
        }

    def _op_status(self, rid) -> dict:
        with self._sessions_lock:
            sessions = list(self._attached_sessions)
        # v1.10.4+: include the daemon's own PID so the status CLI can
        # surface ``pid: <n>`` to the user. The lock file holds the same
        # PID but on Windows ``msvcrt.locking`` blocks reads from other
        # processes, so the daemon is the only authority that can
        # report it reliably while the daemon is alive.
        return {
            "id": rid,
            "ok": True,
            "project": str(self._project_root),
            "pid": os.getpid(),
            "sessions": sessions,
            "open_files": self._engine.open_files(),
            "active_servers": [
                spec.command[0] for spec in self._engine.active_servers()
            ],
            "held_uris": self._lock_manager.held_uris(),
            "compile_aware_languages": (
                sorted(self._compile.runners().keys()) if self._compile else []
            ),
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
