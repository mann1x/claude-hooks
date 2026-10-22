"""Who is asking, and the store for this host — shared by the CLI, the MCP
tools and the hooks.

A claim follows a process on its own host (:mod:`claude_hooks.resources.
store`, "Liveness"). From a session — its Bash tool, its MCP server, its
hooks — that process is the session's **Claude Code process**: the claim
ends when the session does, even if nobody releases it, and survives any
number of short-lived shells in between. The ``run`` wrapper instead
follows its own pid, so a wrapped job's claim ends with the job.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from claude_hooks.resources.store import Owner, ResourceStore, host_name

log = logging.getLogger("claude_hooks.resources")

_STORE: Optional[ResourceStore] = None


def store(config: Optional[dict] = None) -> ResourceStore:
    """One store per process (its connection is reused)."""
    global _STORE
    if _STORE is None:
        _STORE = ResourceStore.from_config(config)
    return _STORE


def reset_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
    _STORE = None


def session_id_here() -> str:
    return (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID", ""))


def alias_for(session_id: str, cwd: Optional[str] = None) -> str:
    """The session's ``/rename`` name, else its project directory's."""
    from claude_hooks.mailbox import identity
    from claude_hooks.mailbox.integration import alias_for as dir_alias
    name = identity.session_name(
        session_id, project_dir=cwd or os.environ.get("CLAUDE_PROJECT_DIR"))
    if name:
        return name
    return dir_alias(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or None)


def owner_here(*, session_id: Optional[str] = None,
               cwd: Optional[str] = None,
               own_process: bool = False,
               follow_pid: Optional[int] = None,
               client: Optional[tuple] = None) -> Owner:
    """The owner for a claim made from here.

    ``own_process``: follow this process (``run``), not the session. With
    neither a Claude Code ancestor nor ``own_process``, the claim follows
    nothing on this host and lives by its lease or TTL alone.
    """
    from claude_hooks._proc import process_start_time
    sid = session_id if session_id is not None else session_id_here()
    cwd = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    pid = started = None
    if follow_pid:
        # A detached job (setsid, nohup) the claim must outlive the
        # session for: the claim ends when that process does.
        pid = int(follow_pid)
        started = process_start_time(pid)
        if started is None:
            raise ValueError(f"no such process {pid}")
    elif own_process:
        pid = os.getpid()
        started = process_start_time(pid)
    else:
        if client is None:
            from claude_hooks.mailbox.identity import client_process
            client = client_process()
        if client:
            pid, started = client
    alias = alias_for(sid, cwd) if sid else os.path.basename(
        os.path.abspath(cwd)) or "shell"
    return Owner(session_id=sid, alias=alias, host=host_name(), pid=pid,
                 pid_started=started, cwd=cwd)


def quiet_holder(host: Optional[str] = None, *,
                 session_id: Optional[str] = None):
    """The claim keeping ``host`` quiet, unless it is ``session_id``'s
    own; None when the host is not quiet. For background work deciding
    whether to start now. Never raises: no store means not quiet."""
    host = (host or host_name()).lower()
    try:
        held = store().snapshot()
    except Exception:
        log.debug("resources: quiet check failed", exc_info=True)
        return None
    for c in held:
        if session_id and c.session_id == session_id:
            continue
        for it in c.items:
            if it.mode == "quiet" and it.resource.split("/")[0] == host:
                return c
    return None
