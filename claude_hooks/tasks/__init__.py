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

    Calls the provider's embedder directly rather than through
    ``embed_for_store``, which holds the provider lock for the whole
    round-trip: on the prompt path that would serialise the task lookup
    behind memory recall instead of overlapping it. The embedders are
    stateless HTTP clients, so calling one outside the lock is safe.

    The model name tags each vector, so a host that switches embedders
    re-embeds its tasks instead of comparing vectors across spaces.
    """
    if getattr(provider, "embed_for_store", None) is None:
        return None, ""

    def _embedder():
        emb = getattr(provider, "_embedder", None)
        if emb is None:
            lock = getattr(provider, "_lock", None)
            if lock is not None:
                with lock:
                    provider._ensure_ready()
            else:
                provider._ensure_ready()
            emb = getattr(provider, "_embedder", None)
        return emb

    def embed(text: str):
        if not str(text or "").strip():
            return None
        try:
            emb = _embedder()
            if emb is None:
                return provider.embed_for_store(text)
            return emb.embed(text)
        except Exception as e:
            log.debug("tasks: embed failed: %s", e)
            return None

    # The vector space is named the way the provider names it: by its
    # table (memories_qwen3), which changes exactly when the embedding
    # model does. Some embedders (llamafile) carry no model name at all.
    opts = getattr(provider, "options", None) or {}
    model = str(opts.get("embedder_model") or opts.get("model") or "")
    if not model:
        try:
            model = str(getattr(_embedder(), "model", "") or "")
        except Exception:
            model = ""
    space = str(opts.get("table") or opts.get("collection") or "")
    if space:
        model = f"{model}@{space}" if model else space
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


def tasks_enabled(config: Optional[dict] = None) -> bool:
    """On unless ``hooks.tasks.enabled`` is false. Default-on on every OS:
    a task list that has to be switched on per host is one the sessions
    that need it never get."""
    if config is None:
        try:
            from claude_hooks.config import load_config
            config = load_config()
        except Exception:
            return True
    section = ((config or {}).get("hooks") or {}).get("tasks") or {}
    return bool(section.get("enabled", True))


def sql_provider(providers):
    """The first provider whose connection can carry the index, or None."""
    from claude_hooks.mailbox.integration import _dialect
    for p in providers or []:
        if _dialect(p) is not None and getattr(p, "_lock", None) is not None:
            return p
    return None
