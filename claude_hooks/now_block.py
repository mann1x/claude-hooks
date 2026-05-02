"""Build the ``## Now`` markdown block that gets prepended to recall
``additionalContext``.

Why this exists
---------------
The assistant has no internal clock — it only knows whatever the
system prompt or tool output happen to surface. ``datetime.now(tz=UTC)``
is what most of our internal code uses (correctly — UTC is the right
canonical for stored data), and that habit leaks into model output:
"I'll cron this for 09:00" turns out to be 09:00 UTC, not 09:00 in the
user's local timezone, and the user sees the wrong time.

Likewise, ETAs ("the build should finish in 30 minutes") drift because
the model anchors on a stale timestamp from earlier tool output rather
than the actual current time.

The fix is structural: every UserPromptSubmit and SessionStart
prepends a one-line "## Now" block with the current local time, the
IANA zone, the UTC offset, and a short reminder to use this block as
the source of truth for anything time-dependent. ~30 tokens per turn.

Configurable
------------
- ``system.now_block.enabled``  — bool, default True
- ``system.now_block.timezone`` — IANA zone name override, default
  null (= system tzlocal). Only useful if the daemon runs on a host
  whose system TZ differs from where the user actually is.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger("claude_hooks.now_block")


def _resolve_tz(tz_override: Optional[str]):
    """Return a tzinfo for the configured zone, falling back to the
    system local zone if the override is unset or invalid."""
    if tz_override:
        try:
            from zoneinfo import ZoneInfo  # py3.9+ stdlib
            return ZoneInfo(tz_override)
        except Exception as e:  # invalid zone name, missing tzdata
            log.debug("now_block: zoneinfo(%r) failed: %s — falling back", tz_override, e)
    # System local — astimezone() with no arg picks up tzlocal.
    return None


def get_cfg(config: dict) -> dict:
    raw = (config.get("system") or {}).get("now_block") or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "timezone": raw.get("timezone") or None,
    }


def format_now_block(config: dict, *, now: Optional[datetime] = None) -> str:
    """Return the markdown block, or empty string when disabled.

    Output shape::

        ## Now

        2026-05-02 16:34:20 CEST (Europe/Berlin, UTC+02:00, Saturday)
        _Anchor any time-dependent reasoning (ETAs, cron / loop /
        monitor / scheduled-wakeup fire times, "in N minutes")
        on this line, NOT on stale timestamps from tool output._
    """
    cfg = get_cfg(config)
    if not cfg["enabled"]:
        return ""

    tz = _resolve_tz(cfg["timezone"])
    if now is None:
        now = datetime.now(tz=tz) if tz is not None else datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone() if tz is None else now.replace(tzinfo=tz)
    else:
        # Already-aware datetime — convert to the configured zone (or
        # to system local if no override) so the displayed time is in
        # the user's local timezone, not whatever zone the caller used.
        now = now.astimezone(tz) if tz is not None else now.astimezone()

    abbrev = now.strftime("%Z") or ""
    iana = cfg["timezone"] or _detect_iana_zone() or ""
    offset = now.strftime("%z")
    if offset and len(offset) == 5:
        offset = f"UTC{offset[:3]}:{offset[3:]}"

    parts = [now.strftime("%Y-%m-%d %H:%M:%S")]
    paren_bits: list[str] = []
    if abbrev:
        # Lead with the human-friendly abbrev outside the paren.
        parts[-1] = f"{parts[-1]} {abbrev}"
    if iana:
        paren_bits.append(iana)
    if offset:
        paren_bits.append(offset)
    paren_bits.append(now.strftime("%A"))
    line = parts[0] + " (" + ", ".join(paren_bits) + ")"

    return (
        "## Now\n\n"
        f"{line}\n"
        "_Anchor any time-dependent reasoning (ETAs, cron / loop / "
        "monitor / scheduled-wakeup fire times, \"in N minutes\") on "
        "this line, NOT on stale timestamps from tool output._"
    )


def _detect_iana_zone() -> Optional[str]:
    """Best-effort lookup of the system's IANA zone name. Returns
    ``None`` if it can't be determined — the abbrev + offset are
    still surfaced in that case."""
    try:
        from pathlib import Path
        # Linux: /etc/localtime is usually a symlink into /usr/share/zoneinfo/<Region>/<City>
        link = Path("/etc/localtime")
        if link.is_symlink():
            target = str(link.resolve())
            marker = "/zoneinfo/"
            i = target.find(marker)
            if i >= 0:
                return target[i + len(marker):]
    except Exception:
        pass
    try:
        # /etc/timezone is a fallback (Debian-family).
        from pathlib import Path
        p = Path("/etc/timezone")
        if p.is_file():
            v = p.read_text(encoding="utf-8").strip()
            if v:
                return v
    except Exception:
        pass
    return None


def prepend_to_context(additional_context: Optional[str], config: dict) -> Optional[str]:
    """Prepend the now-block to an existing ``additionalContext``
    string. Returns the now-block alone when the input is empty, or
    ``None`` when the now-block is disabled and there's no other
    content (so callers can still short-circuit).
    """
    block = format_now_block(config)
    if not block:
        return additional_context if additional_context else None
    if not additional_context:
        return block
    return f"{block}\n\n{additional_context}"
