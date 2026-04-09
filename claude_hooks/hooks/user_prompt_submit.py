"""
UserPromptSubmit handler — recall from all enabled providers and inject
the results as ``additionalContext``.

The model sees the recalled snippets as a markdown block prepended to its
prompt context, identical to how a project's CLAUDE.md gets injected. The
hook never blocks the prompt; on any error it returns no context and the
turn proceeds normally.
"""

from __future__ import annotations

import logging
from typing import Optional

from claude_hooks.providers import Provider
from claude_hooks.providers.base import Memory

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

    include = hook_cfg.get("include_providers")
    if include:
        active = [p for p in providers if p.name in include]
    else:
        active = list(providers)
    if not active:
        return None

    blocks: list[str] = []
    total_hits = 0
    for provider in active:
        provider_cfg = ((config.get("providers") or {}).get(provider.name)) or {}
        k = int(provider_cfg.get("recall_k", 5))
        try:
            mems = provider.recall(prompt, k=k)
        except Exception as e:
            log.warning("provider %s recall failed: %s", provider.name, e)
            continue
        if not mems:
            continue
        for m in mems:
            m.source_provider = provider.name
        total_hits += len(mems)
        blocks.append(_format_block(provider.display_name or provider.name, mems))

    if not blocks:
        return None

    body = "\n\n".join(blocks)
    body = _truncate(body, int(hook_cfg.get("max_total_chars", 4000)))
    additional_context = (
        "## Recalled memory\n\n"
        f"_{total_hits} hit(s) from {len(blocks)} provider(s) — claude-hooks_\n\n"
        f"{body}"
    )

    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }


def _format_block(provider_label: str, memories: list[Memory]) -> str:
    lines = [f"### {provider_label} ({len(memories)})"]
    for m in memories:
        first_line, *rest = m.text.strip().splitlines() or [""]
        lines.append(f"- {first_line}")
        for r in rest:
            lines.append(f"  {r}")
    return "\n".join(lines)


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 100].rstrip() + "\n\n…(truncated)"
