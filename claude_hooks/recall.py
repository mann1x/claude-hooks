"""
Shared recall pipeline.

Extracted from ``user_prompt_submit.py`` so that multiple hooks (UserPromptSubmit,
SessionStart on compact, /reflect) can reuse the same recall logic. Each hook
calls ``run_recall()`` with its own query and formatting preferences.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from claude_hooks.providers import Provider
from claude_hooks.providers.base import Memory

log = logging.getLogger("claude_hooks.recall")


# ---------------------------------------------------------------------- #
# Query clamp
# ---------------------------------------------------------------------- #
# A recall query is spent twice -- once on the embedder, whose cost is
# linear in length and dwarfs every other step in this pipeline, and once
# as HyDE's chat prefill. Neither had an upper bound. ``min_prompt_chars``
# was the only length check anywhere, so a pasted diagnosis or log dump
# was embedded in full.
#
# Measured on solidpc 2026-07-25 against the CPU llamafile embedder:
#
#     1 000 chars   1.4 s        16 000 chars  67.6 s
#     5 000 chars   9.0 s        30 000 chars  ~300 s  (``max_chars``)
#
# The UserPromptSubmit hook is capped at 65 s, and the embedder's own
# timeout is 180 s -- so the embedder never gives up first. A ~15 KB
# prompt loses the *entire* recall for that turn: Claude Code discards
# the hook output, and nothing surfaces beyond a timeout notice.
#
# 3 500 is sized for the worst of the density range rather than for
# average prose: ~1 100 tokens of prose at 3.2 chars/token, ~2 600 tokens
# of base64 at 1.35, both comfortably inside 65 s even on pandorum
# (~2x slower at identical weights -- an OS/build gap, not hardware).
DEFAULT_MAX_QUERY_CHARS = 3500

# Why this is not a prefix cut. A long prompt's signal sits at both ends
# -- the framing at the top, the actual ask at the bottom -- while the
# bulk in between is usually pasted logs, diffs or JSON whose repetitive
# boilerplate drags the embedding toward a generic centroid. Dropping the
# middle is better for recall *quality*, not only for latency, which is
# the whole reason a query cap is worth doing well.
_HEAD_SHARE = 0.65          # framing is denser than the closing ask
_ELLIPSIS = "\n…\n"         # bare marker: "(truncated)" would inject
                            # English tokens that no stored memory has
# Boundaries in descending preference. Cutting mid-word leaves a mangled
# trailing token that is pure noise in the vector.
_BOUNDARIES = ("\n\n", "\n", ". ", " ")
_MAX_BACKTRACK = 0.25       # never sacrifice more than this hunting a
                            # boundary; a minified-JSON prompt has none

# Fenced blocks are squeezed rather than dropped. A pasted traceback is
# often the entire point of the question, and its identifying line -- the
# exception, the failing symbol -- is at the top; what follows is frame
# after frame of boilerplate. Keeping the head preserves the signal and
# sheds the bulk, where dropping the block outright would lose exactly
# the string the user wants matched.
_BLOCK_KEEP_CHARS = 400
_BLOCK_MIN_SQUEEZE = 600    # below this a block is not the problem


def _cut_head(text: str, budget: int) -> str:
    """Trim ``text`` to at most ``budget``, ending on a natural boundary."""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    window = text[:budget]
    floor = int(budget * (1 - _MAX_BACKTRACK))
    for sep in _BOUNDARIES:
        idx = window.rfind(sep)
        if idx >= floor:
            return window[: idx + len(sep)].rstrip()
    return window.rstrip()


def _cut_tail(text: str, budget: int) -> str:
    """Keep at most ``budget`` trailing chars, starting on a boundary."""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    window = text[-budget:]
    ceiling = int(budget * _MAX_BACKTRACK)
    for sep in _BOUNDARIES:
        idx = window.find(sep)
        if 0 <= idx <= ceiling:
            return window[idx + len(sep):].lstrip()
    return window.lstrip()


def _squeeze_fenced_blocks(text: str) -> str:
    """Shorten long ``` fenced blocks to their opening lines."""
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"```[^\n]*\n.*?(?:```|\Z)", text, re.DOTALL):
        block = m.group(0)
        if len(block) <= _BLOCK_MIN_SQUEEZE:
            continue
        out.append(text[pos:m.start()])
        # The cut keeps the opening fence, so the language tag survives.
        out.append(_cut_head(block, _BLOCK_KEEP_CHARS) + _ELLIPSIS)
        pos = m.end()
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def clamp_query(query: str, max_chars: int = DEFAULT_MAX_QUERY_CHARS) -> str:
    """Bound a recall query to ``max_chars``, preserving both ends.

    ``max_chars <= 0`` disables the clamp, matching the convention used
    by ``_truncate`` and the ``ef_search`` provider option. A query
    already inside the budget is returned unchanged and untouched --
    the fast path costs one ``len()``, which matters because this runs
    on every prompt while an actual clamp is rare.
    """
    if max_chars <= 0 or len(query) <= max_chars:
        return query

    squeezed = _squeeze_fenced_blocks(query)
    if len(squeezed) <= max_chars:
        return squeezed

    budget = max_chars - len(_ELLIPSIS)
    head_budget = int(budget * _HEAD_SHARE)
    tail_budget = budget - head_budget
    # head_budget + tail_budget == budget < max_chars <= len(squeezed),
    # so the two windows provably cannot overlap and duplicate content.
    clamped = (_cut_head(squeezed, head_budget) + _ELLIPSIS
               + _cut_tail(squeezed, tail_budget))
    return clamped if len(clamped) <= max_chars else clamped[:max_chars]


def _max_query_chars(hook_cfg: dict) -> int:
    """Read the knob defensively.

    ``.get(key, default)`` rather than ``or``: a configured ``0`` means
    "disable", and ``or`` would silently turn that into the default --
    the same sentinel trap that bit ``ef_search``.
    """
    raw = hook_cfg.get("max_query_chars", DEFAULT_MAX_QUERY_CHARS)
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("invalid max_query_chars %r, using %d",
                    raw, DEFAULT_MAX_QUERY_CHARS)
        return DEFAULT_MAX_QUERY_CHARS


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

    # --- Step 0: Clamp the query ---
    # Applied here rather than at either call site so that both consumers
    # -- the raw embed below and the HyDE prefill in step 2 -- are bounded
    # by one decision. Doing it per-call-site would leave whichever one
    # was added next unbounded again.
    max_query_chars = _max_query_chars(hook_cfg)
    clamped = clamp_query(query, max_query_chars)
    if len(clamped) != len(query):
        log.info("query clamped for recall: %d -> %d chars (max_query_chars=%d)",
                 len(query), len(clamped), max_query_chars)
        query = clamped

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
            model_ref=hook_cfg.get("hyde_model_ref"),
            fallback_model_ref=hook_cfg.get("hyde_fallback_model_ref"),
            timeout=float(hook_cfg.get("hyde_timeout", 30.0)),
            max_tokens=int(hook_cfg.get("hyde_max_tokens", 150)),
            keep_alive=str(hook_cfg.get("hyde_keep_alive", "15m")),
            num_ctx=int(hook_cfg.get("hyde_num_ctx", 16384)),
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
            model_ref=hook_cfg.get("hyde_model_ref"),
            fallback_model_ref=hook_cfg.get("hyde_fallback_model_ref"),
            timeout=float(hook_cfg.get("hyde_timeout", 30.0)),
            max_tokens=int(hook_cfg.get("hyde_max_tokens", 150)),
            keep_alive=str(hook_cfg.get("hyde_keep_alive", "15m")),
            num_ctx=int(hook_cfg.get("hyde_num_ctx", 16384)),
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
