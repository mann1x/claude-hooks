"""
Shared recall pipeline.

Extracted from ``user_prompt_submit.py`` so that multiple hooks (UserPromptSubmit,
SessionStart on compact, /reflect) can reuse the same recall logic. Each hook
calls ``run_recall()`` with its own query and formatting preferences.
"""

from __future__ import annotations

import logging
from typing import Optional

from claude_hooks.providers import Provider
from claude_hooks.providers.base import Memory

log = logging.getLogger("claude_hooks.recall")


def run_recall(
    query: str,
    *,
    config: dict,
    providers: list[Provider],
    hook_name: str = "user_prompt_submit",
    cwd: str = "",
    max_total_chars: int = 4000,
    include_openwolf: bool = True,
    progressive: bool = False,
) -> Optional[str]:
    """
    Run the full recall pipeline and return a formatted additionalContext
    string, or None if nothing was recalled.

    Steps:
      1. (Optional) HyDE query expansion
      2. Recall from all active providers
      3. (Optional) Attention decay re-ranking
      4. Format as markdown
      5. (Optional) Append OpenWolf context
      6. Truncate to budget
    """
    hook_cfg = (config.get("hooks") or {}).get(hook_name) or {}

    # Filter providers.
    include = hook_cfg.get("include_providers")
    if include:
        active = [p for p in providers if p.name in include]
    else:
        active = list(providers)
    if not active:
        return None

    # --- Step 1: HyDE query expansion ---
    search_query = query
    if hook_cfg.get("hyde_enabled"):
        search_query = _hyde_expand(query, hook_cfg)

    # --- Step 2: Recall from providers ---
    blocks: list[str] = []
    all_mems: list[Memory] = []
    total_hits = 0
    for provider in active:
        pcfg = (config.get("providers") or {}).get(provider.name) or {}
        k = int(pcfg.get("recall_k", 5))
        try:
            mems = provider.recall(search_query, k=k)
        except Exception as e:
            log.warning("provider %s recall failed: %s", provider.name, e)
            continue
        if not mems:
            continue
        for m in mems:
            m.source_provider = provider.name
        all_mems.extend(mems)
        total_hits += len(mems)
        blocks.append(format_block(provider.display_name or provider.name, mems, progressive=progressive))

    # --- Step 3: Attention decay re-ranking ---
    # (Applied per-provider above via the block list; cross-provider decay
    #  would need a different approach. Kept simple for v0.2.)
    if hook_cfg.get("decay_enabled") and all_mems:
        try:
            from claude_hooks.decay import update_recalled
            update_recalled(all_mems, config)
        except Exception as e:
            log.debug("decay update skipped: %s", e)

    # --- Step 4: OpenWolf context ---
    if include_openwolf and cwd:
        try:
            from claude_hooks.openwolf import recall_context
            wolf_ctx = recall_context(cwd)
            if wolf_ctx:
                blocks.append(wolf_ctx)
        except Exception as e:
            log.debug("openwolf recall skipped: %s", e)

    if not blocks:
        return None

    # --- Step 5: Assemble and truncate ---
    body = "\n\n".join(blocks)
    body = _truncate(body, max_total_chars)
    return (
        "## Recalled memory\n\n"
        f"_{total_hits} hit(s) from {len([b for b in blocks if b.startswith('###')])} provider(s) — claude-hooks_\n\n"
        f"{body}"
    )


# ---------------------------------------------------------------------- #
# Formatting
# ---------------------------------------------------------------------- #
def format_block(
    provider_label: str,
    memories: list[Memory],
    *,
    progressive: bool = False,
) -> str:
    """Format a provider's memories as a markdown block."""
    lines = [f"### {provider_label} ({len(memories)})"]
    for m in memories:
        text = m.text.strip()
        if not text:
            continue
        first_line, *rest = text.splitlines()
        if progressive and rest:
            extra_chars = sum(len(r) for r in rest)
            lines.append(f"- {first_line}  _({extra_chars}+ chars)_")
        else:
            lines.append(f"- {first_line}")
            for r in rest:
                lines.append(f"  {r}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# HyDE
# ---------------------------------------------------------------------- #
def _hyde_expand(query: str, hook_cfg: dict) -> str:
    """Attempt HyDE query expansion. Returns original query on any failure."""
    try:
        from claude_hooks.hyde import expand_query
        return expand_query(
            query,
            model=hook_cfg.get("hyde_model", "qwen3.5:2b"),
            fallback_model=hook_cfg.get("hyde_fallback_model", "gemma4:e2b"),
            url=hook_cfg.get("hyde_url", "http://localhost:11434/api/generate"),
            timeout=float(hook_cfg.get("hyde_timeout", 30.0)),
            max_tokens=int(hook_cfg.get("hyde_max_tokens", 150)),
            keep_alive=str(hook_cfg.get("hyde_keep_alive", "15m")),
        )
    except Exception as e:
        log.debug("hyde expansion failed: %s", e)
        return query


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 100].rstrip() + "\n\n…(truncated)"
