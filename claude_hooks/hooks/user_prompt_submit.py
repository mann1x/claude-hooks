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
    skip_recall = len(prompt) < min_chars

    additional_context: str = ""
    if not skip_recall:
        from claude_hooks.recall import run_recall
        additional_context = run_recall(
            prompt,
            config=config,
            providers=providers,
            hook_name="user_prompt_submit",
            cwd=event.get("cwd", ""),
            max_total_chars=int(hook_cfg.get("max_total_chars", 4000)),
            progressive=bool(hook_cfg.get("progressive")),
        ) or ""
    else:
        log.debug("prompt too short (%d < %d) — skipping recall", len(prompt), min_chars)

    # Prepend the "## Now" block so the model has a fresh, local-TZ
    # timestamp every turn — anchors ETAs and scheduled-trigger
    # reasoning that would otherwise drift on UTC-only datetime.now().
    from claude_hooks.now_block import prepend_to_context
    final_context = prepend_to_context(additional_context, config)
    if not final_context:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": final_context,
        }
    }
