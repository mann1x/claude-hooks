"""Persistent task tracking (docs/PLAN-task-tracking.md).

One Markdown file per task under ``<project>/.claude-hooks/tasks/`` is
the record; a ``tasks`` table on the store the host already runs
(pgvector or sqlite_vec) indexes it for listing, cross-project views and
recall. Built because Claude Code's own task list is off by default on
current models and garbage-collects what it does keep — a session's
plan should not depend on either.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("claude_hooks.tasks")


def index_for_provider(provider):
    """A :class:`TaskIndex` on ``provider``'s connection, or None."""
    from claude_hooks.mailbox.integration import _dialect
    from claude_hooks.tasks.store import TaskIndex
    dialect = _dialect(provider)
    lock = getattr(provider, "_lock", None)
    if dialect is None or lock is None:
        return None

    def connect():
        provider._ensure_ready()
        return provider._conn

    return TaskIndex(connect, lock, dialect=dialect)


def embedder_for_provider(provider):
    """``(embed(text) -> vec | None, model name)``, or ``(None, "")``.

    The model name tags each vector, so a host that switches embedders
    re-embeds its tasks instead of comparing vectors across spaces.
    """
    embed = getattr(provider, "embed_for_store", None)
    if embed is None:
        return None, ""
    model = ""
    opts = getattr(provider, "options", None) or {}
    for key in ("embedder_model", "model", "embed_model"):
        if opts.get(key):
            model = str(opts[key])
            break
    if not model:
        emb = getattr(provider, "_embedder", None)
        model = str(getattr(emb, "model", "") or "")
    return embed, (model or getattr(provider, "name", "embedder"))


def service_for(provider=None, *, cwd: Optional[str] = None,
                session_id: str = "", embed: bool = True):
    """The task service for the session's project.

    ``provider`` may be None (files only). ``embed=False`` keeps the
    write path free of embedder round-trips; :meth:`embed_pending`
    catches up later.
    """
    from claude_hooks.mailbox.store import host_name
    from claude_hooks.tasks.files import project_root
    from claude_hooks.tasks.service import TaskService
    index = None
    embedder, model = None, ""
    if provider is not None:
        try:
            index = index_for_provider(provider)
        except Exception:
            log.warning("tasks: no index on %r", provider, exc_info=True)
        if index is not None and embed:
            embedder, model = embedder_for_provider(provider)
    return TaskService(project_root(cwd), index, host=host_name(),
                       session_id=session_id, embedder=embedder,
                       embed_model=model)
