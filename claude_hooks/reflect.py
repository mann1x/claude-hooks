"""
/reflect synthesis — analyze recent memories for recurring patterns
and generate CLAUDE.md rules.

Pull recent memories from all providers, group by observation_type,
call Ollama to identify recurring themes, and append new rules to
CLAUDE.md. Inspired by claude-diary's two-stage capture → reflect pipeline.

Can be invoked as:
  - CLI: ``python -m claude_hooks.reflect``
  - Hook: add ``"Reflect"`` to HANDLERS in dispatcher.py
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from claude_hooks.config import expand_user_path, load_config
from claude_hooks.dispatcher import build_providers
from claude_hooks.providers import Provider
from claude_hooks.providers.base import Memory

log = logging.getLogger("claude_hooks.reflect")

_REFLECT_SYSTEM = (
    "You analyze a set of memory entries from an AI coding assistant's sessions. "
    "Find recurring patterns, repeated mistakes, or preferences that appear 2+ times. "
    "For each pattern, write ONE concise rule as an imperative sentence "
    "(e.g., 'Always validate SQL identifiers before interpolation'). "
    "Output ONLY the rules, one per line, prefixed with '- '. "
    "If no patterns are found, output 'No patterns found.'"
)


def reflect(
    config: Optional[dict] = None,
    providers: Optional[list[Provider]] = None,
    *,
    dry_run: bool = False,
) -> list[str]:
    """
    Analyze recent memories and generate CLAUDE.md rules.
    Returns the list of generated rules (empty if none found).
    """
    cfg = config or load_config()
    reflect_cfg = cfg.get("reflect") or {}
    if not reflect_cfg.get("enabled", True):
        return []

    if providers is None:
        providers = build_providers(cfg)
    if not providers:
        log.info("reflect: no providers available")
        return []

    max_memories = int(reflect_cfg.get("max_memories_to_analyze", 50))
    min_count = int(reflect_cfg.get("min_pattern_count", 3))

    # Pull recent memories from all providers.
    all_mems = _pull_recent(providers, max_per_provider=max_memories // max(len(providers), 1))
    if len(all_mems) < min_count:
        log.info("reflect: too few memories (%d < %d)", len(all_mems), min_count)
        return []

    # Group by observation_type if available.
    grouped = _group_by_type(all_mems)

    # Build a text block for Ollama.
    text_block = _format_for_analysis(grouped)

    # Call Ollama to find patterns.
    model = reflect_cfg.get("ollama_model", "gemma4:e2b")
    url = reflect_cfg.get("ollama_url", "http://localhost:11434/api/generate")
    rules = _call_ollama_reflect(text_block, model=model, url=url)

    if not rules:
        log.info("reflect: no patterns found")
        return []

    log.info("reflect: found %d rules", len(rules))

    if dry_run:
        for r in rules:
            print(r)
        return rules

    # Append to CLAUDE.md.
    output_path = expand_user_path(reflect_cfg.get("output_path", "~/.claude/CLAUDE.md"))
    _append_rules(output_path, rules)
    return rules


def _pull_recent(
    providers: list[Provider],
    max_per_provider: int = 25,
) -> list[Memory]:
    """Recall recent memories using broad queries."""
    queries = [
        "recent session patterns and fixes",
        "user preferences and corrections",
        "bugs errors and gotchas",
    ]
    all_mems: list[Memory] = []
    seen_texts: set[str] = set()
    for provider in providers:
        for q in queries:
            try:
                mems = provider.recall(q, k=max_per_provider)
            except Exception:
                continue
            for m in mems:
                key = m.text[:100]
                if key not in seen_texts:
                    seen_texts.add(key)
                    m.source_provider = provider.name
                    all_mems.append(m)
    return all_mems


def _group_by_type(memories: list[Memory]) -> dict[str, list[Memory]]:
    """Group memories by observation_type metadata."""
    groups: dict[str, list[Memory]] = {}
    for m in memories:
        obs_type = m.metadata.get("observation_type", "general")
        groups.setdefault(obs_type, []).append(m)
    return groups


def _format_for_analysis(grouped: dict[str, list[Memory]]) -> str:
    """Format grouped memories as a text block for Ollama analysis."""
    parts = []
    for obs_type, mems in grouped.items():
        parts.append(f"## {obs_type.upper()} ({len(mems)} entries)")
        for m in mems[:20]:
            parts.append(f"- {m.text[:200]}")
    return "\n".join(parts)


def _call_ollama_reflect(text: str, *, model: str, url: str) -> list[str]:
    """Call Ollama to find patterns and return rules."""
    body = json.dumps({
        "model": model,
        "system": _REFLECT_SYSTEM,
        "prompt": f"Analyze these memory entries:\n\n{text}",
        "stream": False,
        "think": False,
        "options": {"num_predict": 500},
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError) as e:
        log.warning("reflect: ollama call failed: %s", e)
        return []

    response = (data.get("response") or "").strip()
    if "no patterns found" in response.lower():
        return []

    rules = [line.strip() for line in response.splitlines() if line.strip().startswith("- ")]
    return rules


def _append_rules(path: Path, rules: list[str]) -> None:
    """Append generated rules to CLAUDE.md under a dated section."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    section = (
        f"\n\n## Auto-reflected rules ({ts})\n\n"
        f"_Generated by claude-hooks /reflect from {len(rules)} pattern(s)._\n\n"
        + "\n".join(rules)
        + "\n"
    )

    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            pass

    # Don't append if we already reflected today.
    if f"## Auto-reflected rules ({ts})" in existing:
        log.info("reflect: already reflected today, skipping append")
        return

    with open(path, "a", encoding="utf-8") as f:
        f.write(section)
    log.info("reflect: appended %d rules to %s", len(rules), path)


# ---------------------------------------------------------------------- #
# CLI entry point
# ---------------------------------------------------------------------- #
def main() -> int:
    import sys
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    dry_run = "--dry-run" in sys.argv
    rules = reflect(dry_run=dry_run)
    if rules:
        print(f"Generated {len(rules)} rules:")
        for r in rules:
            print(f"  {r}")
    else:
        print("No patterns found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
