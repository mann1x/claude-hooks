"""
SessionStart handler — inject a brief status line listing which memory
providers are active. Useful so the user can see at a glance whether
recall will happen this session.
"""

from __future__ import annotations

import logging
from typing import Optional

from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.session_start")


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("session_start") or {}
    if not hook_cfg.get("enabled", True):
        return None
    if not hook_cfg.get("show_status_line", True):
        return None

    if not providers:
        return None

    labels = [(p.display_name or p.name) for p in providers]
    source = (event.get("source") or "startup").lower()
    verb = {"resume": "Resumed", "compact": "Compacted", "startup": "Started"}.get(source, "Started")

    line = (
        f"_{verb} with claude-hooks recall enabled "
        f"({len(providers)} provider(s): {', '.join(labels)})._"
    )

    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": line,
        }
    }
