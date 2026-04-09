"""
Attention decay scoring for recalled memories.

Tracks which memories have been recalled and when. On each recall cycle,
memories that haven't been used recently get a slight penalty; memories
recalled 1-2 times get a boost (validated useful); memories recalled 5+
times get a penalty (stale/over-recalled).

History is stored in a JSON file at ``~/.claude/claude-hooks-decay.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from claude_hooks.config import expand_user_path
from claude_hooks.providers.base import Memory

log = logging.getLogger("claude_hooks.decay")

_MAX_HISTORY_AGE_DAYS = 90


def memory_hash(mem: Memory) -> str:
    """Deterministic hash of a memory's text (first 200 chars)."""
    key = mem.text[:200].strip()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def update_recalled(memories: list[Memory], config: dict) -> None:
    """
    Update the decay history with the memories that were just recalled.
    Called from the recall pipeline after formatting.
    """
    hook_cfg = (config.get("hooks") or {}).get("user_prompt_submit") or {}
    path = expand_user_path(hook_cfg.get("decay_file", "~/.claude/claude-hooks-decay.json"))

    history = _load_history(path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for mem in memories:
        h = memory_hash(mem)
        entry = history.get(h, {"last_recalled": "", "recall_count": 0})
        entry["last_recalled"] = now
        entry["recall_count"] = entry.get("recall_count", 0) + 1
        history[h] = entry

    _prune_old(history)
    _save_history(path, history)


def apply_decay(
    memories: list[Memory],
    config: dict,
) -> list[Memory]:
    """
    Re-rank memories by applying decay scoring. Returns a new sorted list.
    Memories without history get a neutral score.
    """
    hook_cfg = (config.get("hooks") or {}).get("user_prompt_submit") or {}
    path = expand_user_path(hook_cfg.get("decay_file", "~/.claude/claude-hooks-decay.json"))
    halflife = float(hook_cfg.get("decay_recency_halflife_days", 14))
    freq_cap = int(hook_cfg.get("decay_frequency_cap", 5))

    history = _load_history(path)

    scored: list[tuple[float, Memory]] = []
    for mem in memories:
        h = memory_hash(mem)
        entry = history.get(h)
        if entry:
            rb = _recency_boost(entry.get("last_recalled", ""), halflife)
            fb = _frequency_boost(entry.get("recall_count", 0), freq_cap)
            score = rb * fb
        else:
            score = 1.0
        scored.append((score, mem))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [mem for _, mem in scored]


def _recency_boost(last_recalled: str, halflife_days: float) -> float:
    """Memories recalled recently get a boost; old ones fade toward 0.5."""
    if not last_recalled:
        return 1.0
    try:
        last = datetime.fromisoformat(last_recalled)
    except ValueError:
        return 1.0
    now = datetime.now(timezone.utc)
    days_ago = max(0, (now - last).total_seconds() / 86400)
    return 0.5 + 0.5 * math.exp(-0.693 * days_ago / max(halflife_days, 0.1))


def _frequency_boost(recall_count: int, cap: int) -> float:
    """Small boost for validated (1-2 recalls), penalty for over-recalled."""
    if recall_count <= 0:
        return 1.0
    if recall_count <= 2:
        return 1.1
    if recall_count >= cap:
        return 0.6
    cap = max(cap, 3)
    t = (recall_count - 2) / (cap - 2)
    return 1.1 - 0.5 * t


# ---------------------------------------------------------------------- #
# Persistence
# ---------------------------------------------------------------------- #
def _load_history(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("entries", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _save_history(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 1, "entries": entries}
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("failed to save decay history: %s", e)


def _prune_old(entries: dict) -> None:
    """Remove entries older than _MAX_HISTORY_AGE_DAYS."""
    now = datetime.now(timezone.utc)
    to_delete = []
    for h, entry in entries.items():
        lr = entry.get("last_recalled", "")
        if not lr:
            to_delete.append(h)
            continue
        try:
            last = datetime.fromisoformat(lr)
            if (now - last).days > _MAX_HISTORY_AGE_DAYS:
                to_delete.append(h)
        except ValueError:
            to_delete.append(h)
    for h in to_delete:
        del entries[h]
