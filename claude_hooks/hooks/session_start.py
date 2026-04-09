"""
SessionStart handler — inject a brief status line listing which memory
providers are active.

On ``source == "compact"`` (context compaction), also runs the full recall
pipeline so the model recovers its memory after compaction.
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
    if not providers:
        return None

    labels = [(p.display_name or p.name) for p in providers]
    source = (event.get("source") or "startup").lower()
    verb = {"resume": "Resumed", "compact": "Compacted", "startup": "Started"}.get(source, "Started")

    status_line = (
        f"_{verb} with claude-hooks recall enabled "
        f"({len(providers)} provider(s): {', '.join(labels)})._"
    )

    # On compaction, re-inject full recalled context so the model recovers
    # its memory. Without this, all prior hook injections are lost.
    if source == "compact" and hook_cfg.get("compact_recall", True):
        try:
            from claude_hooks.recall import run_recall

            query = hook_cfg.get(
                "compact_recall_query",
                "session context, key decisions, and important patterns",
            )
            recalled = run_recall(
                query,
                config=config,
                providers=providers,
                hook_name="user_prompt_submit",
                cwd=event.get("cwd", ""),
            )
            if recalled:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": f"{status_line}\n\n{recalled}",
                    }
                }
        except Exception as e:
            log.warning("compact recall failed: %s", e)

    if not hook_cfg.get("show_status_line", True):
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": status_line,
        }
    }
