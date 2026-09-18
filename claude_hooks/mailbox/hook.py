"""The hook-side of the mailbox: announce, register, never write.

Three properties this module exists to guarantee, all of them about
*not* doing things:

**No body is ever injected.** See :mod:`claude_hooks.mailbox.announce`.

**No hook blocks on the mailbox.** ``SessionStart`` and
``UserPromptSubmit`` already carry a 65 s cap that the HyDE chain can
consume, and ``Stop`` competes with the store path. Every call here is
wrapped so that a slow or broken mailbox costs nothing — no messages
announced beats a delayed prompt, because the announcement is an
optimisation over the model calling ``mailbox-list`` itself.

**No hook writes on the prompt path.** The announcement is one indexed
SELECT. ``read_at`` is stamped by the ``mailbox-read`` tool, and the
registry upsert happens at ``SessionStart`` only — not per turn.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("claude_hooks.mailbox.hook")

#: Short on purpose. The mailbox is the cheapest thing in the hook and
#: must never be the reason a prompt is late.
QUERY_TIMEOUT_SECONDS = 1.5


def _enabled(config: dict) -> bool:
    return bool((config.get("hooks", {})
                 .get("mailbox", {}) or {}).get("enabled", False))


def _tools(config: dict, providers, event: Optional[dict] = None):
    """Bind the mailbox to the first provider that can carry it."""
    from claude_hooks.mailbox.integration import tools_for_provider
    sid = (event or {}).get("session_id") or ""
    cwd = (event or {}).get("cwd") or None
    for provider in providers or []:
        try:
            tools = tools_for_provider(provider, cwd=cwd, session_id=sid)
        except Exception:
            log.debug("mailbox: provider %s unusable",
                      getattr(provider, "name", "?"), exc_info=True)
            continue
        if tools is not None:
            return tools
    return None


def announce_block(*, event: dict, config: dict, providers,
                   since: Optional[datetime] = None,
                   include_receipts: bool = True) -> str:
    """The ``## Messages`` block, or "" — never an exception.

    ``since`` is what makes the Stop hook safe to add: it limits the
    announcement to messages that arrived *during* this turn, so it
    never repeats what ``UserPromptSubmit`` already showed.
    """
    if not _enabled(config):
        return ""
    try:
        tools = _tools(config, providers, event)
        if tools is None:
            return ""
        from claude_hooks.mailbox.announce import render
        if tools.session_id:
            # Keep this session's registry row fresh. Without it
            # ``last_seen`` only moves at SessionStart, so a session
            # held open for longer than the registry TTL is swept away
            # while somebody is actively using it — and the next sender
            # is told the alias does not exist.
            try:
                tools.store.touch(tools.session_id)
            except Exception:
                log.debug("mailbox: touch failed", exc_info=True)
        messages = tools.store.inbox(
            alias=tools.alias, session_id=tools.session_id or None,
            host=tools.host, since=since)
        receipts = (tools.store.pending_receipts(from_alias=tools.alias,
                                                 from_host=tools.host)
                    if include_receipts else [])
        if not messages and not receipts:
            return ""
        block = render(messages, receipts, alias=tools.alias,
                       host=tools.host)
        if receipts:
            # Announcing a receipt *is* delivering it — the note is
            # already shown in full, so a tool call to mark it read
            # would be ceremony. The stamp is a write, so it goes
            # through the store rather than staying on the hook path's
            # conscience: if it fails the receipt simply repeats next
            # turn, which is the right way round for this to fail.
            try:
                tools.store.mark_receipts_seen(
                    [r["id"] for r in receipts], from_alias=tools.alias,
                    from_host=tools.host)
            except Exception:
                log.debug("mailbox: could not mark receipts seen",
                          exc_info=True)
        return block
    except Exception:
        # Fails silent-open by design; see the module docstring.
        log.debug("mailbox announce failed", exc_info=True)
        return ""


def register_session(*, event: dict, config: dict, providers) -> str:
    """Record this session; return the collision line, or "".

    The collision line is the only warning a *sender* gets before the
    ambiguity refusal surprises them later, and SessionStart is the only
    place it belongs.
    """
    if not _enabled(config):
        return ""
    try:
        tools = _tools(config, providers, event)
        if tools is None or not tools.session_id:
            return ""
        others = tools.store.register(
            tools.session_id, tools.alias, cwd=(event.get("cwd") or ""),
            host=tools.host)
        from claude_hooks.mailbox.announce import collision_note
        return collision_note(others, tools.alias)
    except Exception:
        log.debug("mailbox registration failed", exc_info=True)
        return ""


def turn_start(event: dict) -> Optional[datetime]:
    """When this turn began, for the Stop hook's ``since``.

    Falls back to None — which announces everything unread rather than
    nothing — because a Stop hook that silently announced nothing would
    reintroduce the gap it was added to close.
    """
    raw = event.get("turn_started_at") or event.get("started_at")
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None
