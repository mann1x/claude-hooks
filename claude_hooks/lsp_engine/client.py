"""Hook-side client + spawn helper.

Wraps :class:`claude_hooks.lsp_engine.ipc.IpcClient` with the
high-level operations the daemon understands (``attach``,
``did_open`` / ``did_change`` / ``did_close``, ``diagnostics``,
``status``, ``detach``). Also provides ``connect_or_spawn`` so the
hook doesn't need to think about whether the daemon is already
running for this project — it just calls and gets back a connected
client.

Phase 1 keeps the spawn flow simple: if the socket isn't live, fork
a detached ``python -m claude_hooks.lsp_engine daemon`` and poll for
the socket. Two parallel callers can race; the loser detects "socket
already in use", waits for it to come up (the winner is bringing it
up), and connects.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from claude_hooks.lsp_engine.daemon import (
    lock_path_for,
    socket_path_for,
)
from claude_hooks.lsp_engine.ipc import IpcClient, _is_socket_alive

log = logging.getLogger("claude_hooks.lsp_engine.client")


DEFAULT_SPAWN_WAIT_S = 5.0
DEFAULT_RPC_TIMEOUT_S = 10.0


class LspEngineClient:
    """Convenience wrapper. One per Claude Code session."""

    def __init__(
        self,
        socket_path: str | os.PathLike,
        session_id: str,
        *,
        timeout: float = DEFAULT_RPC_TIMEOUT_S,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._session = session_id
        self._ipc = IpcClient(self._socket_path, timeout=timeout)
        self._attached = False

    @property
    def session_id(self) -> str:
        return self._session

    def connect(self) -> None:
        self._ipc.connect()

    def attach(self) -> None:
        if self._attached:
            return
        self._call("attach")
        self._attached = True

    def detach(self) -> None:
        if not self._attached:
            return
        try:
            self._call("detach")
        finally:
            self._attached = False

    def close(self) -> None:
        try:
            self.detach()
        finally:
            self._ipc.close()

    def did_open(self, path: str | os.PathLike, content: str) -> bool:
        resp = self._call("did_open", path=str(path), content=content)
        return bool(resp.get("opened"))

    def did_change(
        self,
        path: str | os.PathLike,
        content: str,
    ) -> tuple[bool, Optional[str]]:
        """Returns ``(forwarded, queued_behind)``.

        ``forwarded=True`` means the daemon pushed the change to the
        LSP. ``forwarded=False`` means the change is queued behind
        ``queued_behind`` (another session's id) and will land when
        that session's lock releases.
        """
        resp = self._call("did_change", path=str(path), content=content)
        return bool(resp.get("forwarded")), resp.get("queued_behind")

    def did_close(self, path: str | os.PathLike) -> bool:
        resp = self._call("did_close", path=str(path))
        return bool(resp.get("closed"))

    def diagnostics(
        self,
        path: str | os.PathLike,
        *,
        lock_timeout_ms: int = 500,
        diag_timeout_s: float = 2.0,
    ) -> tuple[list[dict], bool]:
        """Returns ``(diagnostics, stale)``. ``stale=True`` means we
        served the owner's view because the affinity lock didn't
        release within ``lock_timeout_ms``.
        """
        resp = self._call(
            "diagnostics",
            path=str(path),
            timeout_ms=lock_timeout_ms,
            diag_timeout_s=diag_timeout_s,
        )
        return list(resp.get("diagnostics") or []), bool(resp.get("stale"))

    def status(self) -> dict:
        return self._call("status")

    def __enter__(self) -> "LspEngineClient":
        self.connect()
        self.attach()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _call(self, op: str, **params) -> dict:
        resp = self._ipc.call(op, session=self._session, **params)
        if not resp.get("ok"):
            raise RuntimeError(
                f"daemon op {op!r} failed: {resp.get('error')}",
            )
        return resp


def connect_or_spawn(
    project_root: str | os.PathLike,
    session_id: str,
    *,
    state_base: Optional[Path] = None,
    spawn_wait_s: float = DEFAULT_SPAWN_WAIT_S,
    spawn_env: Optional[dict] = None,
    log_path: Optional[Path] = None,
) -> LspEngineClient:
    """Return a connected, attached client for the project's daemon,
    spawning the daemon detached if it isn't already running.

    Race-safe: a second caller arriving after the first has called
    ``Popen`` but before the daemon's socket is up will see the lock
    file present (or the socket coming up) and just keep polling.
    """
    sock_path = socket_path_for(project_root, base=state_base)
    if not _is_socket_alive(sock_path):
        _spawn_daemon(
            project_root, state_base=state_base,
            extra_env=spawn_env, log_path=log_path,
        )
        _wait_for_socket(sock_path, deadline=time.monotonic() + spawn_wait_s)

    client = LspEngineClient(sock_path, session_id)
    client.connect()
    client.attach()
    return client


def _spawn_daemon(
    project_root: str | os.PathLike,
    *,
    state_base: Optional[Path] = None,
    extra_env: Optional[dict] = None,
    log_path: Optional[Path] = None,
) -> None:
    """Fork-and-exec a detached daemon. Returns immediately; the
    caller then polls for the socket via ``_wait_for_socket``.
    """
    cmd = [
        sys.executable,
        "-m",
        "claude_hooks.lsp_engine",
        "daemon",
        "--project",
        str(Path(project_root).resolve()),
    ]
    if state_base is not None:
        cmd.extend(["--state-base", str(state_base)])

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)

    log_fd = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = open(log_path, "ab")  # noqa: SIM115 — caller owns lifecycle

    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd if log_fd else subprocess.DEVNULL,
            stderr=log_fd if log_fd else subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            close_fds=True,
        )
    finally:
        # Popen dups the fd into the child; closing here doesn't
        # affect the daemon's open log file.
        if log_fd is not None:
            log_fd.close()


def _wait_for_socket(sock_path: str | os.PathLike, *, deadline: float) -> None:
    sock_path = Path(sock_path)
    while time.monotonic() < deadline:
        if sock_path.exists() and _is_socket_alive(sock_path):
            return
        time.sleep(0.050)
    raise TimeoutError(
        f"daemon socket {sock_path} did not come up in time",
    )


def daemon_pid(
    project_root: str | os.PathLike,
    *,
    state_base: Optional[Path] = None,
) -> Optional[int]:
    """Return the running daemon's PID by reading its lock file, or
    ``None`` if the lock file is absent or unreadable.

    Used by ``status`` commands and tests; not used by the spawn
    flow itself (which trusts ``flock`` for race correctness).
    """
    p = lock_path_for(project_root, base=state_base)
    if not p.is_file():
        return None
    try:
        first_line = p.read_text(encoding="ascii").splitlines()[0]
        return int(first_line.strip())
    except (ValueError, IndexError, OSError):
        return None
