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
      1. Recall from all active providers using the raw query.
         - If HyDE is disabled, this is the only recall pass.
         - If HyDE is enabled + grounded, these raw hits also serve as
           grounding context for the LLM expansion in step 2.
      2. (Optional) HyDE query expansion:
         - ``hyde_grounded``: feed the raw Qdrant hits back to the LLM
           so it can write a hypothetical answer grounded in real
           memories (prevents hallucinations on niche jargon).
         - Otherwise: plain HyDE expansion of the raw prompt.
         If the raw recall returned nothing, HyDE is skipped entirely:
         there is no memory to expand against, and an ungrounded LLM
         would only hallucinate, so we just return nothing.
      3. (Optional) Refined recall using the HyDE-expanded query.
         Results are merged with the raw recall (raw-first, deduped).
      4. (Optional) Attention decay re-ranking
      5. Format as markdown
      6. (Optional) Append OpenWolf context
      7. Truncate to budget
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

    hyde_enabled = bool(hook_cfg.get("hyde_enabled"))
    hyde_grounded = bool(hook_cfg.get("hyde_grounded", True))

    # --- Step 1: Raw recall (with raw query) ---
    raw_hits_by_provider: dict[str, list[Memory]] = {}
    for provider in active:
        pcfg = (config.get("providers") or {}).get(provider.name) or {}
        k = int(pcfg.get("recall_k", 5))
        try:
            mems = provider.recall(query, k=k)
        except Exception as e:
            log.warning("provider %s recall failed: %s", provider.name, e)
            continue
        for m in mems or []:
            m.source_provider = provider.name
        raw_hits_by_provider[provider.name] = list(mems or [])

    total_raw = sum(len(v) for v in raw_hits_by_provider.values())

    # --- Step 2: HyDE expansion (only if raw recall found something) ---
    # If raw recall returned nothing, skip HyDE: there's no relevant
    # memory to ground against, and ungrounded HyDE on niche queries
    # only hallucinates. Return nothing instead of noise.
    hits_by_provider: dict[str, list[Memory]] = dict(raw_hits_by_provider)

    if hyde_enabled and total_raw > 0:
        qdrant_raw = raw_hits_by_provider.get("qdrant") or []
        if hyde_grounded and qdrant_raw:
            # Grounded expansion: feed the raw Qdrant hits to the LLM
            grounding_k = int(hook_cfg.get("hyde_ground_k", 3))
            grounding = [m.text for m in qdrant_raw[:grounding_k]]
            search_query = _hyde_expand_grounded(query, grounding, hook_cfg)
        else:
            # Plain expansion: raw prompt only
            search_query = _hyde_expand(query, hook_cfg)

        # Only do a refined recall if the expansion actually produced
        # something different from the raw query.
        if search_query and search_query != query:
            for provider in active:
                pcfg = (config.get("providers") or {}).get(provider.name) or {}
                k = int(pcfg.get("recall_k", 5))
                try:
                    refined = provider.recall(search_query, k=k)
                except Exception as e:
                    log.warning("provider %s refined recall failed: %s", provider.name, e)
                    continue
                if not refined:
                    continue
                for m in refined:
                    m.source_provider = provider.name
                # Raw-first merge: keep raw hits in order, append refined
                # hits that aren't already present.
                existing = hits_by_provider.get(provider.name, [])
                seen = {m.text.strip() for m in existing if m.text.strip()}
                for m in refined:
                    key = m.text.strip()
                    if key and key not in seen:
                        existing.append(m)
                        seen.add(key)
                # Cap at recall_k after merge so we don't balloon the context.
                hits_by_provider[provider.name] = existing[:k]

    # --- Step 3: Assemble blocks + decay list ---
    blocks: list[str] = []
    all_mems: list[Memory] = []
    total_hits = 0
    for provider in active:
        mems = hits_by_provider.get(provider.name) or []
        if not mems:
            continue
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
    """Attempt plain HyDE query expansion. Returns original query on any failure."""
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


def _hyde_expand_grounded(query: str, memories: list[str], hook_cfg: dict) -> str:
    """Attempt grounded HyDE expansion. Returns original query on any failure."""
    try:
        from claude_hooks.hyde import expand_query_with_context
        return expand_query_with_context(
            query,
            memories,
            model=hook_cfg.get("hyde_model", "qwen3.5:2b"),
            fallback_model=hook_cfg.get("hyde_fallback_model", "gemma4:e2b"),
            url=hook_cfg.get("hyde_url", "http://localhost:11434/api/generate"),
            timeout=float(hook_cfg.get("hyde_timeout", 30.0)),
            max_tokens=int(hook_cfg.get("hyde_max_tokens", 150)),
            keep_alive=str(hook_cfg.get("hyde_keep_alive", "15m")),
            max_context_chars=int(hook_cfg.get("hyde_ground_max_chars", 1500)),
        )
    except Exception as e:
        log.debug("grounded hyde expansion failed: %s", e)
        return query


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 100].rstrip() + "\n\n…(truncated)"
