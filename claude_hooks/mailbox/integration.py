"""Attaching the mailbox to whatever store the host already runs.

The connection is *borrowed*, never opened. On pgvector the provider's
``psycopg`` handle is not thread-safe, is already ``RLock``-guarded, and
has dead-handle detection that a second connection would not inherit —
so going through ``_ensure_ready()`` on every call means a Postgres
restart is survived here for the same reason it is survived there.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from claude_hooks.mailbox.store import MailboxStore, host_name

log = logging.getLogger("claude_hooks.mailbox.integration")

#: Per-project override, for when the directory name is not the useful
#: name. Same file the rest of the per-project config lives in.
_ALIAS_FILE = Path(".claude-hooks") / "mailbox.toml"


def is_headless(config: Optional[dict] = None, env=None) -> bool:
    """A spawned ``claude -p`` / SDK run, not the session the mailbox is
    for (``claude_hooks.session_kind``). The hooks never run there at
    all; this is the same rule for the MCP tools and in-process callers,
    which reach the mailbox without going through ``run.py``."""
    from claude_hooks.session_kind import hooks_allowed
    return not hooks_allowed(config, env)


def alias_for(cwd: Optional[str] = None) -> str:
    """Default alias: the project directory name.

    Chosen so "message the opencoti session" works with nothing
    registered by hand — an addressing scheme that needs setup before
    the first message is one nobody uses.
    """
    root = Path(cwd or os.getcwd()).resolve()
    override = root / _ALIAS_FILE
    if override.is_file():
        try:
            import tomllib
        except ModuleNotFoundError:      # pragma: no cover - py<3.11
            tomllib = None               # type: ignore[assignment]
        if tomllib is not None:
            try:
                data = tomllib.loads(override.read_text(encoding="utf-8"))
                alias = (data.get("alias") or "").strip()
                if alias:
                    return alias
            except Exception:
                log.debug("unreadable %s", override, exc_info=True)
    return root.name or "unknown"


def store_for_provider(provider) -> Optional[MailboxStore]:
    """Build a :class:`MailboxStore` on ``provider``'s connection.

    Returns None for a provider with no SQL handle to borrow, which is
    not an error — a host without pgvector or sqlite_vec simply has no
    mailbox, and saying so beats half-working.
    """
    dialect = _dialect(provider)
    if dialect is None:
        return None
    lock = getattr(provider, "_lock", None)
    if lock is None:
        return None

    def connect():
        provider._ensure_ready()
        return provider._conn

    return MailboxStore(connect, lock, dialect=dialect)


def _dialect(provider) -> Optional[str]:
    name = (getattr(provider, "name", "") or "").lower()
    if "pgvector" in name or "postgres" in name:
        return "postgres"
    if "sqlite" in name:
        return "sqlite"
    return None


def tools_for_provider(provider, *, cwd: Optional[str] = None,
                       session_id: Optional[str] = None):
    """The eight tools bound to this host's identity, or None."""
    store = store_for_provider(provider)
    if store is None:
        return None
    from claude_hooks.mailbox.tools import MailboxTools
    # Claude Code exports CLAUDE_CODE_SESSION_ID (and CLAUDE_PROJECT_DIR)
    # to hooks and MCP children alike (measured on 2.1.280). The old
    # CLAUDE_SESSION_ID is never set, so the MCP tools had no session id
    # and always derived their alias from a directory.
    sid = (session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
           or os.environ.get("CLAUDE_SESSION_ID", ""))
    # Identity is the session id: it keeps the alias it registered with
    # (an explicit rename, or the default it was given then). A directory
    # is only the fallback for a session that has no registration yet,
    # and then the project root, not wherever the session cd'd to.
    alias = None
    try:
        alias = store.registered_alias(sid)
    except Exception:
        log.debug("mailbox: could not read the registered alias",
                  exc_info=True)
    fallback = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or None
    return MailboxTools(store, alias=alias or alias_for(fallback),
                        session_id=sid, host=host_name())
