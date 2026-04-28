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
    # Port 2 from thedotmack/claude-mem: metadata-gated rerank.
    # When metadata_filter is enabled we ask each provider for a bigger
    # candidate set (k * over_fetch_factor), then keep only the memories
    # whose metadata matches the current context (cwd / type / age),
    # then let HyDE / decay rerank only the survivors. This cuts noise
    # from irrelevant projects without losing recall depth.
    filter_cfg = hook_cfg.get("metadata_filter") or {}
    filter_enabled = bool(filter_cfg.get("enabled", False))
    over_fetch = int(filter_cfg.get("over_fetch_factor", 4)) if filter_enabled else 1

    from claude_hooks._parallel import parallel_map

    def _raw_recall(provider):
        pcfg = (config.get("providers") or {}).get(provider.name) or {}
        k = int(pcfg.get("recall_k", 5))
        fetch_k = k * over_fetch
        mems = provider.recall(query, k=fetch_k)
        for m in mems or []:
            m.source_provider = provider.name
        filtered = _apply_metadata_filter(
            list(mems or []), filter_cfg, cwd=cwd,
        ) if filter_enabled else list(mems or [])
        return (provider.name, filtered[:k])

    raw_hits_by_provider: dict[str, list[Memory]] = {}
    raw_results = parallel_map(
        _raw_recall, active,
        on_error=lambda p, e: log.warning(
            "provider %s recall failed: %s", p.name, e,
        ),
    )
    for r in raw_results:
        if r is None:
            continue
        name, mems = r
        raw_hits_by_provider[name] = mems

    total_raw = sum(len(v) for v in raw_hits_by_provider.values())

    # --- Step 2: HyDE expansion (only if raw recall found something) ---
    # If raw recall returned nothing, skip HyDE: there's no relevant
    # memory to ground against, and ungrounded HyDE on niche queries
    # only hallucinates. Return nothing instead of noise.
    hits_by_provider: dict[str, list[Memory]] = dict(raw_hits_by_provider)

    if hyde_enabled and total_raw > 0:
        # Grounding source: prefer the configured semantic-recall backend
        # (qdrant, pgvector, or sqlite_vec — any vector store works). Fall
        # back to whichever provider returned the most raw hits, so
        # grounding stays useful regardless of which backends are enabled.
        configured = hook_cfg.get("hyde_grounding_provider")
        for candidate in (configured, "qdrant", "pgvector", "sqlite_vec"):
            if candidate and candidate in raw_hits_by_provider:
                grounding_provider = candidate
                break
        else:
            grounding_provider = max(
                raw_hits_by_provider,
                key=lambda n: len(raw_hits_by_provider[n]),
                default=None,
            )
        grounding_raw = raw_hits_by_provider.get(grounding_provider) or [] if grounding_provider else []
        if hyde_grounded and grounding_raw:
            grounding_k = int(hook_cfg.get("hyde_ground_k", 3))
            grounding = [m.text for m in grounding_raw[:grounding_k]]
            search_query = _hyde_expand_grounded(query, grounding, hook_cfg)
        else:
            # Plain expansion: raw prompt only
            search_query = _hyde_expand(query, hook_cfg)

        # Only do a refined recall if the expansion actually produced
        # something different from the raw query.
        if search_query and search_query != query:
            def _refined_recall(provider):
                pcfg = (config.get("providers") or {}).get(provider.name) or {}
                k = int(pcfg.get("recall_k", 5))
                refined = provider.recall(search_query, k=k)
                if not refined:
                    return (provider.name, k, [])
                for m in refined:
                    m.source_provider = provider.name
                return (provider.name, k, refined)

            refined_results = parallel_map(
                _refined_recall, active,
                on_error=lambda p, e: log.warning(
                    "provider %s refined recall failed: %s", p.name, e,
                ),
            )
            for r in refined_results:
                if r is None:
                    continue
                name, k, refined = r
                if not refined:
                    continue
                # Raw-first merge: keep raw hits in order, append refined
                # hits that aren't already present.
                existing = hits_by_provider.get(name, [])
                seen = {m.text.strip() for m in existing if m.text.strip()}
                for m in refined:
                    key = m.text.strip()
                    if key and key not in seen:
                        existing.append(m)
                        seen.add(key)
                # Cap at recall_k after merge so we don't balloon the context.
                hits_by_provider[name] = existing[:k]

    # --- Step 3: Assemble blocks + decay list ---
    blocks: list[str] = []
    all_mems: list[Memory] = []
    total_hits = 0
    contributing_provider_labels: list[str] = []
    for provider in active:
        mems = hits_by_provider.get(provider.name) or []
        if not mems:
            continue
        all_mems.extend(mems)
        total_hits += len(mems)
        label = provider.display_name or provider.name
        contributing_provider_labels.append(label)
        blocks.append(format_block(label, mems, progressive=progressive))

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
    if contributing_provider_labels:
        provider_summary = ", ".join(contributing_provider_labels)
    else:
        provider_summary = "0 providers"
    return (
        "## Recalled memory\n\n"
        f"_{total_hits} hit(s) from {provider_summary} — claude-hooks_\n\n"
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
            model=hook_cfg.get("hyde_model", "gemma4:e2b"),
            fallback_model=hook_cfg.get("hyde_fallback_model", "gemma4:e4b"),
            url=hook_cfg.get("hyde_url", "http://localhost:11434/api/generate"),
            timeout=float(hook_cfg.get("hyde_timeout", 30.0)),
            max_tokens=int(hook_cfg.get("hyde_max_tokens", 150)),
            keep_alive=str(hook_cfg.get("hyde_keep_alive", "15m")),
            cache_enabled=bool(hook_cfg.get("hyde_cache_enabled", True)),
            cache_ttl_seconds=int(hook_cfg.get("hyde_cache_ttl_seconds", 86400)),
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
            model=hook_cfg.get("hyde_model", "gemma4:e2b"),
            fallback_model=hook_cfg.get("hyde_fallback_model", "gemma4:e4b"),
            url=hook_cfg.get("hyde_url", "http://localhost:11434/api/generate"),
            timeout=float(hook_cfg.get("hyde_timeout", 30.0)),
            max_tokens=int(hook_cfg.get("hyde_max_tokens", 150)),
            keep_alive=str(hook_cfg.get("hyde_keep_alive", "15m")),
            max_context_chars=int(hook_cfg.get("hyde_ground_max_chars", 1500)),
            cache_enabled=bool(hook_cfg.get("hyde_cache_enabled", True)),
            cache_ttl_seconds=int(hook_cfg.get("hyde_cache_ttl_seconds", 86400)),
        )
    except Exception as e:
        log.debug("grounded hyde expansion failed: %s", e)
        return query


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 100].rstrip() + "\n\n…(truncated)"


# ------------------------------------------------------------------ #
# Metadata filter — port 2 from thedotmack/claude-mem
# ------------------------------------------------------------------ #
def _apply_metadata_filter(
    memories: list[Memory],
    filter_cfg: dict,
    *,
    cwd: str = "",
) -> list[Memory]:
    """Drop memories that don't match the filter criteria.

    Empty / missing metadata fields on a memory never cause a reject —
    we only filter when we have a positive signal, to stay recall-
    friendly. Known keys:

    - ``require_cwd_match: true`` — keep only memories whose
      ``metadata.cwd`` equals the current cwd. When the memory has no
      ``cwd`` at all, it passes (legacy memories aren't penalised).
    - ``require_observation_type: "fix" | "decision" | …`` — keep only
      memories whose ``metadata.observation_type`` matches.
    - ``max_age_days: N`` — drop memories whose ``metadata.stored_at``
      parses to > N days ago. No ``stored_at`` = pass.
    - ``require_tags: [...]`` — keep only memories whose
      ``metadata.tags`` contains at least one of the required tags.
    """
    if not memories:
        return memories

    require_cwd = bool(filter_cfg.get("require_cwd_match"))
    req_type = filter_cfg.get("require_observation_type") or None
    max_age_days = filter_cfg.get("max_age_days")
    req_tags = set(filter_cfg.get("require_tags") or [])

    import datetime as _dt
    cutoff = None
    if isinstance(max_age_days, (int, float)) and max_age_days > 0:
        cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=float(max_age_days))

    out: list[Memory] = []
    for m in memories:
        meta = m.metadata or {}
        # cwd gate — only applies when the memory records a cwd.
        if require_cwd and cwd:
            mcwd = meta.get("cwd")
            if mcwd and mcwd != cwd:
                continue
        # observation_type gate
        if req_type:
            obs_type = meta.get("observation_type")
            if obs_type and obs_type != req_type:
                continue
        # age gate
        if cutoff is not None:
            stored_at = meta.get("stored_at") or meta.get("timestamp")
            if stored_at:
                try:
                    raw = stored_at
                    if isinstance(raw, str) and raw.endswith("Z"):
                        raw = raw[:-1] + "+00:00"
                    ts = _dt.datetime.fromisoformat(raw)
                    if ts.tzinfo is not None:
                        ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
                    if ts < cutoff:
                        continue
                except (TypeError, ValueError):
                    pass
        # tag gate
        if req_tags:
            mtags = set(meta.get("tags") or [])
            if mtags and not (req_tags & mtags):
                continue
        out.append(m)
    return out
