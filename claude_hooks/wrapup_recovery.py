"""Post-compact wrap-up recovery.

When the PreCompact hook fires, ``wrapup_synth`` writes a markdown
state-summary file to disk before Claude Code drops the conversation
window. The hook also emits the markdown as ``additionalContext`` so
the post-compaction model sees it inline, but in practice that inline
context can get summarised away during the very next compaction cycle
or trimmed before the next user turn — and then the model has lost
the connection state (pod IDs, IPs, URLs, in-progress work) that it
needed to resume.

The fix here is small and additive: every UserPromptSubmit we scan
the three known wrap-up directories for any file modified within the
last ``max_age_seconds`` (default 24h), and prepend a short pointer
block to ``additionalContext`` telling the model to read the latest
file. Cheap (one ``os.scandir`` per directory) and the pointer block
is ~5 lines of markdown.

Triggers on **only** the existence of a recent file — if the user
hasn't compacted recently, nothing surfaces. The pointer's job is to
survive across the boundary; once the next session has read the file,
the model has the state it needs and the pointer is just one extra
line of context per turn.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.wrapup_recovery")

_DEFAULT_MAX_AGE_SECONDS = 24 * 3600
_FNAME_PREFIX = "wrapup-pre-compact"


def _candidate_dirs(cwd: str) -> list[Path]:
    out: list[Path] = []
    if cwd:
        out.append(Path(cwd) / ".wolf")
        out.append(Path(cwd) / "docs" / "wrapup")
    out.append(Path.home() / ".claude" / "wrapup-pre-compact")
    return out


def find_recent_wrapup(cwd: str, *,
                       max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
                       now: Optional[float] = None) -> Optional[Path]:
    """Return the most recently-modified pre-compact wrap-up file
    found in any candidate directory, or None if nothing fresh."""
    now = now if now is not None else time.time()
    cutoff = now - max_age_seconds
    best: Optional[tuple[float, Path]] = None
    for d in _candidate_dirs(cwd):
        try:
            if not d.is_dir():
                continue
            for entry in os.scandir(d):
                if not entry.is_file():
                    continue
                name = entry.name
                if not name.startswith(_FNAME_PREFIX) and _FNAME_PREFIX not in name:
                    continue
                if not name.endswith(".md"):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                if best is None or mtime > best[0]:
                    best = (mtime, Path(entry.path))
        except OSError as e:
            log.debug("wrapup_recovery: scandir(%s) failed: %s", d, e)
    return best[1] if best else None


def get_cfg(config: dict) -> dict:
    raw = (config.get("hooks") or {}).get("wrapup_recovery") or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "max_age_seconds": int(raw.get("max_age_seconds", _DEFAULT_MAX_AGE_SECONDS)),
    }


def format_recovery_block(cwd: str, config: dict, *,
                          now: Optional[float] = None) -> str:
    """Return a short markdown pointer block, or empty string when
    disabled or no recent wrap-up file exists."""
    cfg = get_cfg(config)
    if not cfg["enabled"]:
        return ""
    path = find_recent_wrapup(cwd, max_age_seconds=cfg["max_age_seconds"], now=now)
    if path is None:
        return ""
    return (
        "## Pre-compact wrap-up available\n\n"
        f"A pre-compact state summary was saved to `{path}`. "
        "If this is a freshly-resumed session after auto-compaction, "
        "**read that file first** to recover open items, in-progress work, "
        "pod / host connection state, and next steps. The inline "
        "`additionalContext` from the PreCompact hook may have been "
        "trimmed; the file on disk is authoritative."
    )
