"""
Memory deduplication.

Before storing a new memory, checks if a near-duplicate already exists in the
provider. Uses text-based similarity (``difflib.SequenceMatcher``) on the top
recall result to avoid storing redundant entries.
"""

from __future__ import annotations

import difflib
import logging
from typing import Optional

from claude_hooks.providers.base import Memory, Provider

log = logging.getLogger("claude_hooks.dedup")


def should_store(
    content: str,
    provider: Provider,
    *,
    threshold: float = 0.85,
    k: int = 3,
) -> bool:
    """
    Return True if ``content`` is sufficiently novel to store.
    Returns True on any error (fail-open: better to store a dup than lose data).
    """
    if not content.strip():
        return False

    try:
        existing = provider.recall(content[:500], k=k)
    except Exception as e:
        log.debug("dedup recall failed for %s: %s", provider.name, e)
        return True

    if not existing:
        return True

    for mem in existing:
        sim = text_similarity(content, mem.text)
        if sim >= threshold:
            log.debug(
                "dedup: %.2f similarity with existing memory (threshold %.2f), skipping",
                sim, threshold,
            )
            return False

    return True


def text_similarity(a: str, b: str) -> float:
    """Quick text similarity using SequenceMatcher on first 500 chars."""
    return difflib.SequenceMatcher(None, a[:500], b[:500]).ratio()
