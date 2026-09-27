"""Status-line segments: usage limits and unread mail.

The status line is the one surface Claude Code refreshes while a session
sits idle (``"refreshInterval"`` in the ``statusLine`` setting), and it
costs no tokens. Hooks only run on a prompt, a tool call or a Stop, so a
message that arrives while the operator is away is invisible to them —
and asking the model "did I get mail?" spends a turn to find out.

**Usage** comes from the claude-hooks proxy when it is running (its state
is fresh), else from the ``rate_limits`` block Claude Code itself passes
to the status line; :func:`native_state` puts the latter in the proxy
state's shape, so ``scripts/statusline_compose.py`` renders both through
the same formatter and the line looks the same whichever source answered.

**Mail** is this session's unread count, cached per session for
:data:`MAIL_CACHE_SECONDS`: the status line re-runs on every assistant
message, and a database round-trip per message is not free. Everything
here returns ``None`` / ``""`` on failure — a status line must never
break.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("claude_hooks.statusline")

#: How long an unread count is reused. Short enough that mail shows up
#: within one idle refresh or two, long enough that a burst of assistant
#: messages costs one query.
MAIL_CACHE_SECONDS = 20

#: Sessions end without telling the status line; drop their caches.
MAIL_CACHE_MAX_AGE_SECONDS = 86400


# ---------------------------------------------------------------- #
# Usage
# ---------------------------------------------------------------- #

def _pct(block) -> Optional[float]:
    if not isinstance(block, dict):
        return None
    v = block.get("used_percentage")
    return float(v) if isinstance(v, (int, float)) else None


def native_state(payload: dict, *, now: Optional[_dt.datetime] = None) -> dict:
    """Claude Code's ``rate_limits`` in the proxy state's shape, or {}.

    ``used_percentage`` is 0-100; the proxy state holds 0-1 fractions.
    The binding window is the fuller one — the proxy gets it from
    Anthropic's ``representative-claim`` header, which the status line
    payload does not carry.
    """
    rl = (payload or {}).get("rate_limits")
    if not isinstance(rl, dict):
        return {}
    five, seven = _pct(rl.get("five_hour")), _pct(rl.get("seven_day"))
    if five is None and seven is None:
        return {}
    state: dict = {}
    if five is not None:
        state["five_hour_utilization"] = five / 100.0
    if seven is not None:
        state["seven_day_utilization"] = seven / 100.0
    state["representative_claim"] = (
        "seven_day" if five is None or (seven is not None and seven > five)
        else "five_hour")
    now = now or _dt.datetime.utcnow()
    state["last_updated"] = now.replace(microsecond=0).isoformat() + "Z"
    return state


# ---------------------------------------------------------------- #
# Mail
# ---------------------------------------------------------------- #

def _session_and_cwd(payload: dict) -> tuple[str, str]:
    ws = (payload or {}).get("workspace") or {}
    cwd = (ws.get("project_dir") or ws.get("current_dir")
           or (payload or {}).get("cwd") or "")
    return (payload or {}).get("session_id") or "", cwd


def _cache_path(cache_dir: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:128]
    return cache_dir / f"statusline-{safe}.json"


def _default_cache_dir() -> Path:
    from claude_hooks.mailbox.hook import nudge_state_dir
    return nudge_state_dir()


def lookup_unread(session_id: str, cwd: str,
                  config: Optional[dict] = None) -> Optional[int]:
    """Unread messages for this session, straight from the store.

    None when the mailbox is off here — disabled in config, or a disable
    marker that does not keep ``mailbox`` — or unreachable.
    """
    from claude_hooks.config import load_config
    from claude_hooks.dispatcher import build_providers
    from claude_hooks.hook_parts import kept_parts
    from claude_hooks.mailbox.integration import tools_for_provider
    cfg = config if config is not None else load_config()
    if not ((cfg.get("hooks") or {}).get("mailbox") or {}).get("enabled"):
        return None
    marker = cfg.get("disable_marker_filename") or ".claude-hooks-disable"
    kept = kept_parts(cwd, marker)
    if kept is not None and "mailbox" not in kept:
        return None
    for provider in build_providers(cfg):
        try:
            tools = tools_for_provider(provider, cwd=cwd or None,
                                       session_id=session_id)
        except Exception:
            continue
        if tools is None:
            continue
        return tools.store.inbox_count(alias=tools.alias,
                                       session_id=tools.session_id or None,
                                       host=tools.host)
    return None


def unread_count(payload: dict, *, cache_dir: Optional[Path] = None,
                 ttl: float = MAIL_CACHE_SECONDS,
                 lookup: Callable[[str, str], Optional[int]] = lookup_unread,
                 now: Optional[float] = None) -> Optional[int]:
    """This session's unread count, reused for ``ttl`` seconds.

    A failed lookup is cached too (as None), so a store that is down
    costs one timeout per ``ttl``, not one per assistant message.
    """
    session_id, cwd = _session_and_cwd(payload)
    if not session_id:
        return None
    now = time.time() if now is None else now
    try:
        path = _cache_path(cache_dir or _default_cache_dir(), session_id)
    except Exception:
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if 0 <= now - float(cached["at"]) < ttl:
            n = cached.get("count")
            return n if isinstance(n, int) else None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        count = lookup(session_id, cwd)
    except Exception:
        log.debug("statusline: unread lookup failed", exc_info=True)
        count = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"at": now, "count": count}), encoding="utf-8")
        tmp.replace(path)
        for old in path.parent.glob("statusline-*.json"):
            try:
                if now - old.stat().st_mtime > MAIL_CACHE_MAX_AGE_SECONDS:
                    old.unlink()
            except OSError:
                pass
    except OSError:
        pass
    return count


def mail_segment(count: Optional[int], *, fmt: str) -> str:
    """``📬 2`` / ``mail:2`` — nothing when there is no unread mail.

    ``fmt`` is the already-effective glyph style (emoji / ascii / plain).
    """
    if not count:
        return ""
    if fmt == "emoji":
        return f"📬 {count}"
    return f"mail:{count}"
