"""
SessionEnd handler — fires when a session terminates (user `/exit`,
window closed, or process killed).

Currently a no-op placeholder. Reserved for flushing any future buffered
state (e.g., aggregated metrics, deferred storage). Kept as a hook entry
so the dispatcher can later add behaviour without touching settings.json.
"""

from __future__ import annotations

import logging
from typing import Optional

from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.session_end")


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("session_end") or {}
    if not hook_cfg.get("enabled", True):
        return None
    log.debug("session ended: %s", event.get("session_id"))
    return None
