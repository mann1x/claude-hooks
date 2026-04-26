"""
Companion-tool coordinator — routes to whichever heavy code-graph engine
the user has installed.

Currently supports:

- **axon** (https://github.com/harshkedia177/axon) — the recommended
  companion. Pure-Python install (``pip install axoniq``), KuzuDB-
  backed, MCP tools (``query``, ``context``, ``impact``, ``dead_code``,
  ``cypher``), watcher mode, dead-code detection. Best fit for
  Python/JS/TS repos. See :mod:`claude_hooks.axon_integration`.

- **gitnexus** (https://github.com/abhigyanpatwari/GitNexus) — the
  multi-language alternative. 14 languages, 16 MCP tools, multi-repo
  ``group_*`` queries. Pick when you write languages outside Python/JS/TS
  or coordinate multiple repos. See :mod:`claude_hooks.gitnexus_integration`.

This module owns nothing of its own — it composes the two engine
modules and decides which to call based on what's installed/indexed.

Public API:

    status(root) -> dict
    reindex_if_dirty_async(*, cwd, turn_modified, ...) -> None
    session_start_hint(root) -> Optional[str]
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from claude_hooks import axon_integration as axon
from claude_hooks import gitnexus_integration as gitnexus

log = logging.getLogger("claude_hooks.companion_integration")


def status(root: Path) -> dict:
    """Detection summary for both engines, side-by-side."""
    return {
        "axon": axon.status(root),
        "gitnexus": gitnexus.status(root),
    }


def reindex_if_dirty_async(
    *,
    cwd: str,
    turn_modified: bool,
    lock_min_age_seconds: int = 60,
) -> None:
    """Trigger reindex on whichever engine(s) have indexed this project.

    Both can coexist. We call both engines' reindex helpers; each one
    silently no-ops when its tool is missing or the project isn't
    indexed for that tool. Cooldown locks are per-engine, so triggering
    one doesn't block the other.
    """
    try:
        axon.reindex_if_dirty_async(
            cwd=cwd,
            turn_modified=turn_modified,
            lock_min_age_seconds=lock_min_age_seconds,
        )
    except Exception as e:
        log.debug("axon reindex skipped: %s", e)
    try:
        gitnexus.reindex_if_dirty_async(
            cwd=cwd,
            turn_modified=turn_modified,
            lock_min_age_seconds=lock_min_age_seconds,
        )
    except Exception as e:
        log.debug("gitnexus reindex skipped: %s", e)


def session_start_hint(root: Path) -> Optional[str]:
    """Combined hint mentioning whichever engines are present.

    Returns None when neither tool is installed AND the project isn't
    indexed by either. When both are around, axon comes first (we
    recommend it for Python/JS/TS repos — see COMPANION_TOOLS.md).
    """
    parts: list[str] = []
    a = axon.session_start_hint(root)
    if a:
        parts.append(a)
    g = gitnexus.session_start_hint(root)
    if g:
        parts.append(g)
    return "\n\n".join(parts) if parts else None
