"""
Autonomous memory consolidation — compress old memories, merge duplicates,
and prune stale entries.

Consolidation requires deletion, because merging and compressing both
mean "replace N rows with fewer". A provider that cannot delete is
reported as ``skipped`` rather than counted as work: storing a summary
next to the original grows the corpus this pass exists to shrink.

- ``pgvector`` / ``sqlite_vec`` — full consolidation (``delete_by_hashes``).
- ``qdrant`` — no delete API; every candidate is skipped.
- ``memory_kg`` — has ``delete_entities``, but not the by-hash memory
  delete this pass uses, so it skips too.

Until v1.14.1 this module reported work it had not done: ``merged``
counted *candidates* and never merged, every compression was computed
by an LLM and discarded, and ``pruned`` was never incremented at all.

Can be invoked as:
  - CLI: ``python -m claude_hooks.consolidate``
  - SessionStart hook (if configured with ``trigger: "session_start"``)
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from claude_hooks.config import expand_user_path, load_config
from claude_hooks.dedup import text_similarity
from claude_hooks.dispatcher import build_providers
from claude_hooks.providers import Provider
from claude_hooks.providers.base import Memory

log = logging.getLogger("claude_hooks.consolidate")


@dataclass
class ConsolidationResult:
    merged: int = 0
    compressed: int = 0
    pruned: int = 0
    errors: list[str] = field(default_factory=list)
    #: Work identified but not performed because the provider cannot
    #: delete. Kept separate from the counters above on purpose: until
    #: v1.14.1 ``merged`` reported *candidates* and ``compressed``
    #: reported summaries that were computed and thrown away, so a run
    #: that changed nothing announced "merged=12 compressed=8". A
    #: number that means "considered" must never share a name with one
    #: that means "done".
    skipped: int = 0

    def describe(self) -> str:
        parts = [f"merged={self.merged}", f"compressed={self.compressed}",
                 f"pruned={self.pruned}"]
        if self.skipped:
            parts.append(f"skipped={self.skipped} (provider cannot delete)")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


def consolidate(
    config: Optional[dict] = None,
    providers: Optional[list[Provider]] = None,
    *,
    dry_run: bool = False,
) -> ConsolidationResult:
    """Run the consolidation pipeline."""
    cfg = config or load_config()
    con_cfg = cfg.get("consolidate") or {}
    if not con_cfg.get("enabled", False):
        return ConsolidationResult()

    if providers is None:
        providers = build_providers(cfg)
    if not providers:
        return ConsolidationResult()

    result = ConsolidationResult()
    max_scan = int(con_cfg.get("max_memories_to_scan", 200))
    merge_threshold = float(con_cfg.get("merge_similarity_threshold", 0.80))

    # Pull memories using broad queries.
    all_mems = _pull_all(providers, max_scan)
    if len(all_mems) < 5:
        log.info("consolidate: too few memories (%d), skipping", len(all_mems))
        return result

    # Deletion is what makes consolidation consolidation. Without it
    # the pass can only *add* a compressed copy next to the original,
    # which grows the corpus it was asked to shrink — so a provider
    # that can't delete gets counted as skipped, not done.
    deleters = {id(p): _can_delete(p) for p in providers}
    if not any(deleters.values()):
        log.warning(
            "consolidate: no provider supports deletion (%s) — nothing to do",
            ", ".join(p.name for p in providers) or "none",
        )

    # Merge near-duplicates: keep the longer text, delete the other.
    pairs = _find_merge_candidates(all_mems, threshold=merge_threshold)
    for a, b in pairs:
        keep, drop = (a, b) if len(a.text) >= len(b.text) else (b, a)
        prov = _provider_for(drop, providers)
        if prov is None or not deleters.get(id(prov)):
            result.skipped += 1
            continue
        log.info("consolidate: merging '%s...' into '%s...'",
                 drop.text[:50], keep.text[:50])
        if dry_run:
            result.merged += 1
            continue
        try:
            if _delete_memory(prov, drop):
                result.merged += 1
            else:
                result.skipped += 1
        except Exception as e:              # noqa: BLE001 - reported
            result.errors.append(f"merge delete failed: {e}")

    # Compress long memories: store the summary, then remove the
    # original. Both halves or neither — a stored summary whose original
    # survives is a duplicate, and a deleted original whose summary
    # never stored is data loss.
    # ``model_ref`` (v1.5+) takes precedence over ``ollama_model``.
    model = con_cfg.get("model_ref") or con_cfg.get("ollama_model", "gemma4:e2b")
    url = con_cfg.get("ollama_url", "http://localhost:11434/api/generate")
    num_ctx = int(con_cfg.get("num_ctx", 16384))
    merged_ids = {m.metadata.get("_hash") for _, m in pairs} if pairs else set()
    for mem in all_mems:
        if len(mem.text) <= 1000:
            continue
        if mem.metadata.get("_hash") in merged_ids:
            continue                        # already removed as a duplicate
        prov = _provider_for(mem, providers)
        if prov is None or not deleters.get(id(prov)):
            # Don't pay for an LLM call we cannot act on. The old code
            # ran one per oversized memory and discarded every result.
            result.skipped += 1
            continue
        if dry_run:
            result.compressed += 1
            continue
        compressed = _compress(mem.text, model=model, url=url, num_ctx=num_ctx)
        if not compressed or len(compressed) >= len(mem.text) * 0.7:
            continue
        try:
            prov.store(compressed, metadata=_carry_metadata(mem))
            if _delete_memory(prov, mem):
                result.compressed += 1
                log.debug("consolidate: compressed %d→%d chars",
                          len(mem.text), len(compressed))
            else:
                # Summary landed, original stayed: a duplicate, not a
                # loss. Say so rather than counting it as compressed.
                result.errors.append(
                    "compressed copy stored but original not deleted "
                    f"(id={mem.metadata.get('_hash')})"
                )
        except Exception as e:              # noqa: BLE001 - reported
            result.errors.append(f"compress failed: {e}")

    # Prune stale memories.
    prune_days = int(con_cfg.get("prune_stale_days", 0) or 0)
    if prune_days > 0:
        result.pruned += _prune_stale(providers, deleters, prune_days,
                                      result, dry_run=dry_run)

    # Update state.
    if not dry_run:
        state_path = expand_user_path(
            con_cfg.get("state_file", "~/.claude/claude-hooks-consolidate.json")
        )
        _update_state(state_path)

    log.info("consolidate: %s", result.describe())
    return result


def _can_delete(provider: Provider) -> bool:
    """Whether this provider can actually remove a memory."""
    return callable(getattr(provider, "delete_by_hashes", None))


def _provider_for(mem: Memory, providers: list) -> Optional[Provider]:
    """The provider a recalled memory came from.

    ``source_provider`` is stamped by the dispatcher during fan-out.
    Falls back to the sole provider when there is only one, and
    otherwise returns None — deleting from the wrong store is worse
    than not deleting.
    """
    src = getattr(mem, "source_provider", "") or ""
    for p in providers:
        if p.name == src:
            return p
    return providers[0] if len(providers) == 1 else None


def _delete_memory(provider: Provider, mem: Memory) -> bool:
    """Delete one recalled memory by its ``_hash``. False if unaddressable."""
    hash_hex = (mem.metadata or {}).get("_hash")
    if not hash_hex:
        return False
    try:
        blob = bytes.fromhex(str(hash_hex))
    except ValueError:
        return False
    return int(provider.delete_by_hashes([blob]) or 0) > 0


def _carry_metadata(mem: Memory) -> dict:
    """Metadata for a compressed replacement.

    Underscore-prefixed keys are per-recall annotations (``_hash``,
    ``_table``, ``_score``) — carrying them into a stored row would
    persist one query's ranking as if it were a property of the memory.
    """
    md = {k: v for k, v in (mem.metadata or {}).items()
          if not str(k).startswith("_")}
    md["consolidated"] = True
    md["consolidated_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    return md


def _prune_stale(providers: list, deleters: dict, prune_days: int,
                 result: ConsolidationResult, *, dry_run: bool) -> int:
    """Delete memories whose TTL lapsed more than ``prune_days`` ago.

    Only touches rows that carry an explicit ``expires_at`` — a memory
    with no TTL was never promised a lifetime, and age alone is not
    staleness.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=prune_days)
    pruned = 0
    for p in providers:
        if not deleters.get(id(p)) or not callable(
                getattr(p, "expire_before", None)):
            continue
        try:
            rows = p.expire_before(before_iso=cutoff.isoformat(), limit=500)
        except Exception as e:              # noqa: BLE001 - reported
            result.errors.append(f"{p.name} expire_before failed: {e}")
            continue
        hashes = [bytes(r.content_hash) for r in rows
                  if getattr(r, "content_hash", None)]
        if not hashes:
            continue
        if dry_run:
            pruned += len(hashes)
            continue
        try:
            pruned += int(p.delete_by_hashes(hashes) or 0)
        except Exception as e:              # noqa: BLE001 - reported
            result.errors.append(f"{p.name} prune failed: {e}")
    return pruned


def should_run(config: dict) -> bool:
    """Check whether consolidation should run this session."""
    con_cfg = config.get("consolidate") or {}
    if not con_cfg.get("enabled", False):
        return False
    if con_cfg.get("trigger") != "session_start":
        return False

    state_path = expand_user_path(
        con_cfg.get("state_file", "~/.claude/claude-hooks-consolidate.json")
    )
    min_sessions = int(con_cfg.get("min_sessions_between_runs", 10))
    return _sessions_since_last(state_path) >= min_sessions


# ---------------------------------------------------------------------- #
# Internals
# ---------------------------------------------------------------------- #
def _pull_all(providers: list[Provider], max_total: int) -> list[Memory]:
    """Pull memories using broad queries."""
    queries = ["session", "fix", "decision", "error", "preference", "project"]
    all_mems: list[Memory] = []
    seen: set[str] = set()
    per_provider = max(max_total // max(len(providers), 1), 10)
    for provider in providers:
        for q in queries:
            try:
                mems = provider.recall(q, k=per_provider)
            except Exception:
                continue
            for m in mems:
                key = m.text[:100]
                if key not in seen:
                    seen.add(key)
                    # Stamp provenance here, the only point that knows
                    # it. Recall doesn't set it (the dispatcher does
                    # that during hook fan-out, and this path bypasses
                    # the dispatcher), and consolidation deletes — so
                    # an unattributed memory is one we must skip rather
                    # than guess a store for.
                    if not getattr(m, "source_provider", ""):
                        m.source_provider = provider.name
                    all_mems.append(m)
            if len(all_mems) >= max_total:
                break
    return all_mems[:max_total]


def _find_merge_candidates(
    memories: list[Memory],
    threshold: float = 0.80,
) -> list[tuple[Memory, Memory]]:
    """Find pairs of memories that are similar enough to merge."""
    pairs: list[tuple[Memory, Memory]] = []
    n = len(memories)
    # O(n²) but n is capped at max_memories_to_scan (200).
    for i in range(min(n, 100)):
        for j in range(i + 1, min(n, 100)):
            sim = text_similarity(memories[i].text, memories[j].text)
            if sim >= threshold:
                pairs.append((memories[i], memories[j]))
    return pairs


def _compress(
    text: str, *, model: str, url: str, num_ctx: int = 16384,
) -> Optional[str]:
    """Use the configured chat backend (Ollama or llamafile://) to
    compress a long memory into a shorter summary. ``llamafile://<label>``
    refs dispatch through ``chat_backend``; bare refs use the legacy
    Ollama ``/api/generate`` path."""
    system = "Compress this memory entry to under half its length while keeping all key facts."
    user = text[:2000]
    if model.startswith("llamafile://"):
        from claude_hooks import chat_backend
        out = chat_backend.call(
            user_prompt=user,
            system_prompt=system,
            model_ref=model,
            ollama_url=url,
            timeout=10.0,
            max_tokens=300,
            num_ctx=num_ctx,
        )
        return out or None

    options: dict = {"num_predict": 300}
    if num_ctx and num_ctx > 0:
        options["num_ctx"] = int(num_ctx)
    body = json.dumps({
        "model": model,
        "system": system,
        "prompt": user,
        "stream": False,
        "think": False,
        "options": options,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, socket.timeout, OSError):
        return None
    return (data.get("response") or "").strip() or None


def _update_state(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {"last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"), "session_count": 0}
    try:
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("failed to update consolidation state: %s", e)


def _sessions_since_last(state_path: Path) -> int:
    if not state_path.exists():
        return 999  # Never run before.
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return int(data.get("session_count", 999))
    except (json.JSONDecodeError, OSError):
        return 999


# ---------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------- #
def main() -> int:
    import sys
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()
    # Force enable for CLI invocation.
    cfg.setdefault("consolidate", {})["enabled"] = True
    result = consolidate(cfg, dry_run=dry_run)
    print(f"Consolidation: {result.describe()}")
    for err in result.errors:
        print(f"  error: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
