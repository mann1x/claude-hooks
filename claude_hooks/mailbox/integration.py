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
                       session_id: Optional[str] = None,
                       transcript_path: Optional[str] = None,
                       client=None):
    """The eight tools bound to this session's identity, or None.

    The identity is resolved again on every tool call (see
    :func:`resolve_identity`), so a ``/rename`` mid-session takes effect
    at the next mailbox operation, whoever performs it.
    """
    store = store_for_provider(provider)
    if store is None:
        return None
    from claude_hooks.mailbox.tools import MailboxTools
    # Claude Code exports CLAUDE_CODE_SESSION_ID (and CLAUDE_PROJECT_DIR)
    # to hooks and MCP children alike (measured on 2.1.280). The old
    # CLAUDE_SESSION_ID is never set.
    explicit = bool(session_id)
    sid = (session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
           or os.environ.get("CLAUDE_SESSION_ID", ""))
    fallback = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or None
    host = host_name()
    cached_client = []

    def resolver():
        if not cached_client:
            if client is not None:
                cached_client.append(tuple(client) if client else None)
            else:
                from claude_hooks.mailbox.identity import client_process
                cached_client.append(client_process())
        return resolve_identity(
            store, session_id=sid, project_dir=fallback, host=host,
            transcript_path=transcript_path, client=cached_client[0],
            sid_authoritative=explicit, claim=not is_headless())

    alias, rsid, note = resolver()
    tools = MailboxTools(store, alias=alias, session_id=rsid, host=host)
    tools.identity_note = note
    tools.resolver = resolver
    return tools


def resolve_identity(store, *, session_id: str, project_dir: Optional[str],
                     host: str, transcript_path: Optional[str] = None,
                     client=None, sid_authoritative: bool = True,
                     claim: bool = True) -> tuple[str, str, str]:
    """``(alias, session_id, note)`` for this session, claiming the
    alias in the registry when it changed.

    The order is the user's word first:

    1. The session's name — what ``/rename`` set
       (:func:`claude_hooks.mailbox.identity.session_name`).
    2. Otherwise the alias this session id already holds.
    3. Otherwise ``.claude-hooks/mailbox.toml``, then the project
       directory's name.

    A name is never taken from a live session: :meth:`MailboxStore.claim`
    gives this one ``<name>-2``, ``-3``, … instead, and only a holder that
    has ended, or this same Claude Code process (``/clear``), is
    replaced. ``note`` says so, in one line, when the alias is not the
    one asked for or has just changed; otherwise it is "".

    An MCP child's session id comes from its environment, which still
    names the old session after ``/clear``. When that id has no row
    (``sid_authoritative`` False), the newest row of the same Claude Code
    process is the current session and is adopted.
    """
    from claude_hooks.mailbox import identity
    row = None
    try:
        row = store.registration(session_id) if session_id else None
        if row is None and client and not sid_authoritative:
            adopted = store.registration_for_client(client, host)
            if adopted is not None:
                row = adopted
                session_id = adopted["session_id"]
    except Exception:
        log.debug("mailbox: could not read the registration", exc_info=True)
    name = identity.session_name(session_id, transcript_path=transcript_path,
                                 project_dir=project_dir)
    if name:
        wanted = name
    elif row is not None:
        return row["alias"], session_id, ""
    else:
        wanted = alias_for(project_dir)
    if not session_id or not claim:
        return ((row or {}).get("alias") or wanted), session_id, ""
    if row is not None and row["alias"] == wanted:
        return wanted, session_id, ""
    try:
        res = store.claim(session_id, wanted, host=host,
                          cwd=project_dir or "", client=client)
    except Exception:
        log.debug("mailbox: could not claim %s", wanted, exc_info=True)
        return ((row or {}).get("alias") or wanted), session_id, ""
    return res["alias"], session_id, identity_note(res, host)


def identity_note(res: dict, host: str) -> str:
    """One line when the alias differs from the one asked for, or moved."""
    alias, wanted, prev = res["alias"], res["wanted"], res.get("previous")
    parts = []
    if prev and prev != alias:
        moved = res.get("moved") or 0
        parts.append(f"Mailbox: renamed `{prev}@{host}` → `{alias}@{host}`"
                     + (f"; {moved} unread message"
                        f"{'s' if moved != 1 else ''} moved with it"
                        if moved else "") + ".")
    if alias != wanted and prev != alias:
        parts.append(f"Mailbox: you are `{alias}@{host}` — `{wanted}` "
                     f"belongs to another live session on {host}.")
    return " ".join(parts)
