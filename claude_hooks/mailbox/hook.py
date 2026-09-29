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
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("claude_hooks.mailbox.hook")

#: Short on purpose. The mailbox is the cheapest thing in the hook and
#: must never be the reason a prompt is late.
QUERY_TIMEOUT_SECONDS = 1.5


def _enabled(config: dict) -> bool:
    """On in config, and this is a session someone is working in — never
    a ``claude -p`` / SDK run (see ``integration.is_headless``)."""
    if not bool((config.get("hooks", {})
                 .get("mailbox", {}) or {}).get("enabled", False)):
        return False
    from claude_hooks.mailbox.integration import is_headless
    return not is_headless(config)


def project_dir(event: Optional[dict]) -> Optional[str]:
    """The *fallback* for a session's alias, and nothing more.

    A session's identity is its session id; its alias is whatever it
    registered under (an explicit rename in ``.claude-hooks/mailbox.toml``
    or, failing that, this default) and every later lookup goes through
    ``registered_alias(session_id)``. The default is only computed when
    a session id registers for the first time — which also happens after
    ``/clear``, with the session sitting wherever it last cd'd. It used
    to come from ``event["cwd"]``, so xollama registered as
    ``v0.34.4-xollama.1`` and opencoti as ``llamafile``. It now comes
    from ``CLAUDE_PROJECT_DIR`` (where the session was started; ``run.py``
    copies it into the event because the daemon cannot see the caller's
    env), and ``cwd`` only when that is missing.
    """
    ev = event or {}
    return (ev.get("claude_project_dir") or os.environ.get("CLAUDE_PROJECT_DIR")
            or ev.get("cwd") or None)


def _tools(config: dict, providers, event: Optional[dict] = None):
    """Bind the mailbox to the first provider that can carry it."""
    from claude_hooks.mailbox.integration import tools_for_provider
    sid = (event or {}).get("session_id") or ""
    root = project_dir(event)
    for provider in providers or []:
        try:
            tools = tools_for_provider(provider, cwd=root, session_id=sid)
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
                # Pass the identity too: if this row has been evicted for
                # being quiet, the touch re-creates it rather than
                # leaving a live session unaddressable for the rest of
                # its life.
                tools.store.touch(
                    tools.session_id, alias=tools.alias, host=tools.host,
                    cwd=project_dir(event) or "")
            except Exception:
                log.debug("mailbox: touch failed", exc_info=True)
        # One page, not the backlog: a session holding hundreds of
        # unread messages would otherwise get all of them injected into
        # every prompt. The rest are a count and a pointer to the pager.
        from claude_hooks.mailbox.filters import ANNOUNCE_MAX
        page = tools.store.inbox_page(
            alias=tools.alias, session_id=tools.session_id or None,
            host=tools.host, since=since, page_size=ANNOUNCE_MAX)
        messages = page.rows
        receipts = (tools.store.pending_receipts(from_alias=tools.alias,
                                                 from_host=tools.host)
                    if include_receipts else [])
        # Receipts are marked seen once announced, so capping them just
        # spreads a pile of them over the next few turns.
        receipts = list(receipts)[:ANNOUNCE_MAX]
        if not messages and not receipts:
            return ""
        block = render(messages, receipts, alias=tools.alias,
                       host=tools.host, total=page.total)
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
            tools.session_id, tools.alias, cwd=project_dir(event) or "",
            host=tools.host)
        from claude_hooks.mailbox.announce import collision_note
        return collision_note(others, tools.alias)
    except Exception:
        log.debug("mailbox registration failed", exc_info=True)
        return ""


def unregister_session(*, event: dict, config: dict, providers) -> None:
    """Drop this session from the registry at SessionEnd.

    Soft-fails like every other mailbox hook path: a mailbox that cannot
    be reached must not fail the hook.
    """
    if not _enabled(config):
        return
    try:
        tools = _tools(config, providers, event)
        if tools is None or not tools.session_id:
            return
        tools.store.forget(tools.session_id)
    except Exception:
        log.debug("mailbox unregistration failed", exc_info=True)


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


def unread_messages(*, event: dict, config: dict, providers,
                    since: Optional[datetime] = None) -> list:
    """The unread rows themselves (for the Stop nudge), or [] — never an
    exception. Read-only: no touch, no receipts; announce_block does
    those on the same Stop."""
    if not _enabled(config):
        return []
    try:
        tools = _tools(config, providers, event)
        if tools is None:
            return []
        return list(tools.store.inbox(
            alias=tools.alias, session_id=tools.session_id or None,
            host=tools.host, since=since))
    except Exception:
        log.debug("mailbox unread lookup failed", exc_info=True)
        return []


#: Per-session record of messages the Stop hook has already nudged
#: about. Small and local: a nudge is a hint to one session, not mailbox
#: state, so it does not belong in the shared store.
NUDGE_STATE_MAX_IDS = 500
NUDGE_STATE_MAX_AGE_SECONDS = 7 * 86400


def nudge_state_dir():
    import os
    from pathlib import Path
    override = os.environ.get("CLAUDE_HOOKS_MAILBOX_STATE_DIR")
    return Path(override) if override else Path.home() / ".claude" / "claude-hooks-mailbox"


def claim_nudge(session_id: str, ids) -> list:
    """The ids in ``ids`` this session has not been nudged about yet, and
    record them as nudged. One nudge per message per session: a session
    that reads the mail, or decides not to, is never asked twice.
    Fails closed (returns []): a broken state file must not turn into a
    nudge on every Stop."""
    import json
    import re
    import time
    ids = [i for i in ids if i is not None]
    if not session_id or not ids:
        return []
    try:
        d = nudge_state_dir()
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:128]
        path = d / f"nudged-{safe}.json"
        try:
            seen = set(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            seen = set()
        fresh = [i for i in ids if i not in seen]
        if fresh:
            keep = (list(seen) + fresh)[-NUDGE_STATE_MAX_IDS:]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(keep), encoding="utf-8")
            tmp.replace(path)
        # Sessions end without telling us; drop their records eventually.
        now = time.time()
        for old in d.glob("nudged-*.json"):
            try:
                if now - old.stat().st_mtime > NUDGE_STATE_MAX_AGE_SECONDS:
                    old.unlink()
            except OSError:
                pass
        return fresh
    except Exception:
        log.debug("mailbox nudge state failed", exc_info=True)
        return []
