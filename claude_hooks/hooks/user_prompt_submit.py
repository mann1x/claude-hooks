"""
UserPromptSubmit handler — recall from all enabled providers and inject
the results as ``additionalContext``.

Delegates to the shared :mod:`claude_hooks.recall` pipeline so the same
logic is reused by the compact-recall path in ``session_start.py``.
"""

from __future__ import annotations

import logging
from typing import Optional

from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.user_prompt_submit")


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("user_prompt_submit") or {}
    if not hook_cfg.get("enabled", True):
        return None

    prompt = (event.get("prompt") or "").strip()
    min_chars = int(hook_cfg.get("min_prompt_chars", 30))
    if len(prompt) < min_chars:
        log.debug("prompt too short (%d < %d) — skipping recall", len(prompt), min_chars)
        return None

    from claude_hooks.recall import run_recall

    additional_context = run_recall(
        prompt,
        config=config,
        providers=providers,
        hook_name="user_prompt_submit",
        cwd=event.get("cwd", ""),
        max_total_chars=int(hook_cfg.get("max_total_chars", 4000)),
        progressive=bool(hook_cfg.get("progressive")),
    )
    if not additional_context:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }
