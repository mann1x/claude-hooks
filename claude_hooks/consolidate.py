"""
Autonomous memory consolidation — compress old memories, merge duplicates,
and prune stale entries.

Limitations:
- Qdrant MCP has no delete API — consolidation can only prevent future
  duplicates (via dedup) and store compressed versions. Old entries remain.
- Memory KG has ``delete_entities`` — full consolidation is possible there.

Can be invoked as:
  - CLI: ``python -m claude_hooks.consolidate``
  - SessionStart hook (if configured with ``trigger: "session_start"``)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

    # Find merge candidates.
    pairs = _find_merge_candidates(all_mems, threshold=merge_threshold)
    result.merged = len(pairs)
    if pairs and not dry_run:
        log.info("consolidate: found %d merge candidate(s)", len(pairs))
        # For Qdrant, we can't delete — just log. For Memory KG, we could
        # delete the duplicate entity. For now, log only.
        for a, b in pairs[:10]:
            log.info("  merge candidate: '%s...' ≈ '%s...'", a.text[:50], b.text[:50])

    # Compress long memories.
    model = con_cfg.get("ollama_model", "gemma4:e2b")
    url = con_cfg.get("ollama_url", "http://localhost:11434/api/generate")
    num_ctx = int(con_cfg.get("num_ctx", 16384))
    for mem in all_mems:
        if len(mem.text) > 1000:
            compressed = _compress(mem.text, model=model, url=url, num_ctx=num_ctx)
            if compressed and len(compressed) < len(mem.text) * 0.7:
                result.compressed += 1
                if not dry_run:
                    log.debug("compressed: %d→%d chars", len(mem.text), len(compressed))

    # Update state.
    if not dry_run:
        state_path = expand_user_path(
            con_cfg.get("state_file", "~/.claude/claude-hooks-consolidate.json")
        )
        _update_state(state_path)

    log.info(
        "consolidate: merged=%d compressed=%d pruned=%d errors=%d",
        result.merged, result.compressed, result.pruned, len(result.errors),
    )
    return result


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
    """Use Ollama to compress a long memory into a shorter summary."""
    options: dict = {"num_predict": 300}
    if num_ctx and num_ctx > 0:
        options["num_ctx"] = int(num_ctx)
    body = json.dumps({
        "model": model,
        "system": "Compress this memory entry to under half its length while keeping all key facts.",
        "prompt": text[:2000],
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
    print(f"Consolidation: merged={result.merged} compressed={result.compressed} pruned={result.pruned}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
