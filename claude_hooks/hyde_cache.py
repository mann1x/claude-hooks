"""Persistent cache for HyDE expansions (Tier 1.2 latency reduction).

The slowest step in the recall pipeline is the Ollama HyDE call —
typical 0.5–4 s per UserPromptSubmit. Same (prompt, model, grounding)
input always produces the same expansion, so a cache hit skips Ollama
entirely and brings hook latency down to just the recall round-trips.

Cache shape:
- File: ``~/.claude/claude-hooks-hyde-cache.json`` (configurable)
- Key:  SHA-256 of ``model || \\x00 || grounding || \\x00 || prompt``
- Val:  ``{expansion, ts}`` where ``ts`` is epoch seconds.
- TTL:  default 24 h (configurable). Past TTL is treated as miss.
- Cap:  default 200 entries. LRU eviction when exceeded.

Atomicity: writes go to ``<path>.tmp`` then ``os.replace``. Concurrent
hook invocations may stomp on each other's writes — last-writer wins,
which is fine because each entry is independent. We never corrupt the
file even when two hooks race.

Stdlib-only. No dependency.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.hyde_cache")

DEFAULT_CACHE_PATH = Path.home() / ".claude" / "claude-hooks-hyde-cache.json"
DEFAULT_TTL_SECONDS = 24 * 3600
DEFAULT_MAX_ENTRIES = 200


def _key(prompt: str, model: str, grounding: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update((grounding or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(prompt.encode("utf-8"))
    return h.hexdigest()


def _load(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("hyde cache save failed: %s", exc)


def get(
    prompt: str, model: str, grounding: str = "",
    *,
    path: Path = DEFAULT_CACHE_PATH,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: Optional[float] = None,
) -> Optional[str]:
    """Return the cached expansion or None on miss / expired / corrupt."""
    data = _load(path)
    entry = data.get(_key(prompt, model, grounding))
    if not isinstance(entry, dict):
        return None
    expansion = entry.get("expansion")
    ts = entry.get("ts", 0)
    if not isinstance(expansion, str) or not expansion:
        return None
    now_ts = now if now is not None else time.time()
    if now_ts - ts > ttl_seconds:
        return None
    return expansion


def put(
    prompt: str, model: str, expansion: str, *,
    grounding: str = "",
    path: Path = DEFAULT_CACHE_PATH,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    now: Optional[float] = None,
) -> None:
    """Store an expansion. LRU-evicts when ``max_entries`` is exceeded."""
    if not expansion:
        return
    data = _load(path)
    key = _key(prompt, model, grounding)
    now_ts = now if now is not None else time.time()
    data[key] = {"expansion": expansion, "ts": now_ts}
    if len(data) > max_entries:
        # Drop oldest by ts. ``data`` is a dict, but we sort entries.
        sorted_entries = sorted(
            data.items(),
            key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0,
        )
        keep = sorted_entries[-max_entries:]
        data = dict(keep)
    _save(path, data)


def clear(path: Path = DEFAULT_CACHE_PATH) -> None:
    """Delete the cache file. Used by tests and the /reflect / /consolidate
    pipeline when memory layout changes invalidate prior expansions."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.debug("hyde cache clear failed: %s", exc)
