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

import json
import logging
import os
from pathlib import Path
from typing import Optional

from claude_hooks import axon_integration as axon
from claude_hooks import gitnexus_integration as gitnexus

log = logging.getLogger("claude_hooks.companion_integration")


def _engines_enabled_for_cwd(cwd: Path) -> Optional[set[str]]:
    """Return the set of MCP server keys effective for ``cwd`` per
    ``~/.claude.json`` (root-level mcpServers ∪ per-project mcpServers).

    Returns None when the config file can't be read — callers should
    treat that as "fall back to engine-binary detection only" (the
    previous behaviour). An empty set means a readable config that
    explicitly enables nothing for this cwd.

    The path-key match is exact-string against ``str(cwd)``; Claude Code
    normalises project keys to absolute paths matching the cwd it's
    launched from, so we don't try to be clever with prefix matching.
    """
    home = Path(os.path.expanduser("~")) / ".claude.json"
    try:
        c = json.loads(home.read_text(encoding="utf-8"))
    except Exception as e:
        log.debug("could not read %s for per-project engine check: %s", home, e)
        return None
    keys: set[str] = set((c.get("mcpServers") or {}).keys())
    project = (c.get("projects") or {}).get(str(cwd)) or {}
    keys |= set((project.get("mcpServers") or {}).keys())
    return keys


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


def session_start_hint(root: Path, *, config: Optional[dict] = None) -> Optional[str]:
    """Combined hint mentioning whichever engines are present.

    Returns None when neither tool is installed AND the project isn't
    indexed by either. When both are around, axon comes first (we
    recommend it for Python/JS/TS repos — see COMPANION_TOOLS.md).

    Two behaviours configurable via ``config["hooks"]["companions"]``:

    - ``show_engine_init_hints`` (default ``False``) — when an engine
      binary is installed but the project isn't indexed for it, suppress
      the "Run `<engine> init` ..." nag. The positive "indexed for this
      repo" hint always renders. Default is False because the engines
      ship in user-level ``~/.claude.json`` ``mcpServers`` and the nag
      otherwise fires on every project the user hasn't explicitly opted
      into.
    - ``per_project_mcp_filter`` (default ``True``) — only emit hints
      for engines that appear in the effective ``mcpServers`` for
      ``cwd`` (root-level ∪ per-project). Set False to fall back to
      "any engine the binary detects" — the old behaviour.
    """
    cfg = ((config or {}).get("hooks") or {}).get("companions") or {}
    show_init_hint = bool(cfg.get("show_engine_init_hints", False))
    per_project_filter = bool(cfg.get("per_project_mcp_filter", True))

    enabled_engines: Optional[set[str]] = None
    if per_project_filter:
        enabled_engines = _engines_enabled_for_cwd(root)
    # When the filter is off OR we couldn't read ~/.claude.json, treat
    # both engines as enabled — falls back to the prior detection-only
    # logic and never breaks a user who runs without ~/.claude.json.
    axon_enabled = enabled_engines is None or "axon" in enabled_engines
    gitnexus_enabled = enabled_engines is None or "gitnexus" in enabled_engines

    parts: list[str] = []
    a = axon.session_start_hint(
        root, show_init_hint=show_init_hint, enabled=axon_enabled,
    )
    if a:
        parts.append(a)
    g = gitnexus.session_start_hint(
        root, show_init_hint=show_init_hint, enabled=gitnexus_enabled,
    )
    if g:
        parts.append(g)
    return "\n\n".join(parts) if parts else None
