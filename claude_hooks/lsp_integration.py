"""Shared helpers for wiring the bundled ``lsp_engine`` daemon into
the SessionStart / PostToolUse / SessionEnd hooks (v1.9+).

The three hook handlers stay thin: they read ``hooks.lsp_engine.*``
config, ask this module to do the engine-facing work, and merge the
returned markdown blocks into their existing ``additionalContext``
output. Every helper here soft-fails to ``None`` (or an empty result)
on any dependency failure — the hook chain must never block on an
LSP-engine issue.

Design notes:

- **Short-lived clients.** Each hook opens its own ``LspEngineClient``
  scoped to one call, runs its op, and closes. The daemon stays up
  across calls. The per-file affinity lock keys on ``session_id``,
  not on the client object — multiple short-lived clients sharing
  the same session_id are exactly equivalent to one long-lived
  client. Avoiding state across hook invocations keeps the
  integration trivial to reason about.

- **Session ID.** Pulled from the hook payload's ``session_id`` field
  (Claude Code provides one); falls back to a deterministic-per-pid
  string when absent (rare). The fallback intentionally encodes the
  process and time so two simultaneous hooks in the same project
  don't collide on the same affinity lock.

- **Timeouts.** All engine calls honor the ``diagnostics_timeout_ms``
  and ``diagnostics_wait_s`` from config. ``connect_or_spawn`` honors
  ``spawn_timeout_s``. Soft-fail beyond those — log a warning, return
  empty, never raise out of the hook.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from claude_hooks.config import expand_user_path
from claude_hooks.lsp_engine import (
    LspEngineClient,
    connect_or_spawn,
    socket_path_for,
)

log = logging.getLogger("claude_hooks.lsp_integration")


# --------------------------------------------------------------------- #
# Config gates
# --------------------------------------------------------------------- #

def lsp_engine_cfg(cfg: dict) -> dict:
    """Return the ``hooks.lsp_engine`` block or an empty dict."""
    return (cfg.get("hooks") or {}).get("lsp_engine") or {}


def engine_enabled(cfg: dict) -> bool:
    """Master toggle. ``False`` short-circuits every code path here."""
    return bool(lsp_engine_cfg(cfg).get("enabled", False))


# --------------------------------------------------------------------- #
# Session ID
# --------------------------------------------------------------------- #

def session_id_for_event(event: dict) -> str:
    """Pull ``session_id`` from the hook event payload. Fall back to a
    deterministic-per-pid string when the field is missing or empty.

    The fallback shape (``hook-<pid>-<ms>``) is intentionally not the
    empty string — passing an empty session_id to the engine is a
    valid but confusing call that registers a session with no name.
    """
    sid = event.get("session_id") or ""
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return f"hook-{os.getpid()}-{int(time.time() * 1000)}"


# --------------------------------------------------------------------- #
# Daemon spawn (SessionStart)
# --------------------------------------------------------------------- #

def spawn_engine_safely(
    *,
    project_root: str | os.PathLike,
    session_id: str,
    cfg: dict,
) -> Optional[LspEngineClient]:
    """Run ``connect_or_spawn`` and return the resulting client, or
    ``None`` on any failure.

    Caller is responsible for ``.close()``-ing the client when done.
    """
    eng_cfg = lsp_engine_cfg(cfg)
    try:
        return connect_or_spawn(
            project_root=project_root,
            session_id=session_id,
            state_base=_resolve_state_base(eng_cfg),
            spawn_wait_s=float(eng_cfg.get("spawn_timeout_s", 5.0)),
            log_path=_resolve_log_path(eng_cfg),
        )
    except (TimeoutError, OSError, RuntimeError) as e:
        log.warning("lsp_engine: connect_or_spawn failed: %s", e)
        return None


def _resolve_state_base(eng_cfg: dict) -> Optional[Path]:
    raw = eng_cfg.get("state_base")
    if not raw:
        return None
    return Path(expand_user_path(str(raw)))


def _resolve_log_path(eng_cfg: dict) -> Optional[Path]:
    raw = eng_cfg.get("log_path")
    if not raw:
        return None
    return Path(expand_user_path(str(raw)))


# --------------------------------------------------------------------- #
# Lightweight client (PostToolUse / SessionEnd)
# --------------------------------------------------------------------- #

def open_client_safely(
    *,
    project_root: str | os.PathLike,
    session_id: str,
    cfg: dict,
) -> Optional[LspEngineClient]:
    """Open a non-spawning client against the project's daemon socket.

    Returns ``None`` if the socket is absent or refuses the connection
    (daemon was reaped, never spawned, crashed, etc). Honest about
    not retrying — if the caller wants to spawn-on-demand instead,
    they should call :func:`spawn_engine_safely`.
    """
    try:
        sock = socket_path_for(
            project_root, base=_resolve_state_base(lsp_engine_cfg(cfg)),
        )
    except Exception as e:
        log.warning("lsp_engine: socket_path_for failed: %s", e)
        return None

    if not _socket_present(sock):
        log.debug("lsp_engine: daemon socket absent at %s", sock)
        return None

    try:
        client = LspEngineClient(sock, session_id)
        client.connect()
        client.attach()
        return client
    except (OSError, ConnectionError, RuntimeError) as e:
        log.warning("lsp_engine: open_client failed: %s", e)
        return None


def _socket_present(sock: Any) -> bool:
    """OS-portable existence check. On POSIX the socket is a real
    inode; on Windows it's a named pipe and ``Path.exists`` doesn't
    work — we attempt a stat and accept any result that doesn't
    raise FileNotFoundError."""
    try:
        return Path(str(sock)).exists()
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------- #
# Markdown block builders
# --------------------------------------------------------------------- #

_SEVERITY_LABEL = {
    1: "error",
    2: "warning",
    3: "info",
    4: "hint",
}


def format_diagnostics_block(
    *,
    path: str | os.PathLike,
    diagnostics: list[dict],
    stale: bool,
    cwd: Optional[str | os.PathLike] = None,
    max_per_file: int = 50,
) -> Optional[str]:
    """Build the markdown block PostToolUse adds to ``additionalContext``.

    Returns ``None`` when there's nothing to surface (no diagnostics).
    Mirrors the shape of ``post_tool_use._run_ruff``'s output so the
    model treats the two layers uniformly.
    """
    if not diagnostics:
        return None

    p = Path(str(path))
    display = _relative_or_absolute(p, cwd)

    truncated = diagnostics[:max_per_file]
    overflow = len(diagnostics) - len(truncated)

    lines: list[str] = []
    for d in truncated:
        line = int(d.get("line") or 0) + 1     # LSP positions are 0-based
        col = int(d.get("character") or 0) + 1
        sev = _SEVERITY_LABEL.get(int(d.get("severity") or 2), "warning")
        msg = (d.get("message") or "").strip().splitlines()[0] if d.get("message") else "(no message)"
        source = (d.get("source") or "").strip()
        src_suffix = f"[{source}]" if source else ""
        code = (d.get("code") or "")
        code_suffix = f" ({code})" if code else ""
        lines.append(f"{p.name}:{line}:{col}: {sev}{src_suffix} {msg}{code_suffix}")

    body = "\n".join(lines)
    parts = [
        f"## LSP diagnostics — `{display}`",
        "",
        "```",
        body,
        "```",
    ]
    if overflow > 0:
        parts.append(f"\n_… and {overflow} more (truncated at "
                     f"`max_diagnostics_per_file = {max_per_file}`)._")
    if stale:
        parts.append("\n_(stale: another session holds the affinity "
                     "lock for this file)_")
    return "\n".join(parts)


def format_session_start_status(client: LspEngineClient) -> Optional[str]:
    """One-line status row appended to SessionStart's status block.

    Returns ``None`` when there's nothing useful to show — e.g.
    engine is up but no servers are configured (likely missing
    ``cclsp.json``). We don't print "0 servers" because that's noise
    a user will inevitably misread as a problem.
    """
    try:
        status = client.status()
    except (OSError, RuntimeError) as e:
        log.warning("lsp_engine: status RPC failed: %s", e)
        return None

    servers = status.get("active_servers") or []
    sessions = status.get("sessions") or []
    if not servers:
        return None

    names = ", ".join(_pretty_server_name(s) for s in servers)
    n_sessions = len(sessions)
    sess_label = "session" if n_sessions == 1 else "sessions"
    return (
        f"LSP engine: running ({len(servers)} server"
        f"{'' if len(servers) == 1 else 's'}: {names}; "
        f"{n_sessions} {sess_label})"
    )


def _pretty_server_name(cmd: Any) -> str:
    """``active_servers`` may contain either a command-vector or a
    space-joined string depending on engine version. Normalise to
    the first token (the binary name)."""
    if isinstance(cmd, (list, tuple)):
        return str(cmd[0]) if cmd else "?"
    s = str(cmd).strip()
    if not s:
        return "?"
    return s.split()[0]


def _relative_or_absolute(
    p: Path, cwd: Optional[str | os.PathLike],
) -> str:
    if cwd is None:
        return str(p)
    try:
        return str(p.relative_to(Path(str(cwd))))
    except ValueError:
        return str(p)


__all__ = [
    "engine_enabled",
    "format_diagnostics_block",
    "format_session_start_status",
    "lsp_engine_cfg",
    "open_client_safely",
    "session_id_for_event",
    "spawn_engine_safely",
]
