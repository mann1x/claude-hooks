"""
gitnexus integrator — detect and surface https://github.com/abhigyanpatwari/GitNexus
when the user has it installed, without making it a hard dependency.

This module owns gitnexus detection + reindex spawn. When you want
combined behaviour across both supported engines (gitnexus + axon),
import :mod:`claude_hooks.companion_integration` instead — it routes
to the right engine based on what's installed/indexed for the project.

Silent no-op when gitnexus is missing.

Public API:

    is_available() -> bool
    is_indexed(root: Path) -> bool
    binary_path() -> Optional[str]
    status(root: Path) -> dict
    reindex_if_dirty_async(*, cwd, turn_modified, ...) -> None
    session_start_hint(root: Path) -> Optional[str]
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.gitnexus")

_LOCK_FILENAME = ".gitnexus-reindex.lock"
_DEFAULT_LOCK_MIN_AGE_SECONDS = 60


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def binary_path() -> Optional[str]:
    """Path to the gitnexus binary, or None."""
    return shutil.which("gitnexus")


def _global_registry() -> Optional[Path]:
    p = Path.home() / ".gitnexus" / "registry.json"
    return p if p.exists() else None


def _project_index_dir(root: Path) -> Path:
    return root / ".gitnexus"


def is_available() -> bool:
    """True if gitnexus appears to be installed on this machine."""
    if binary_path() is not None:
        return True
    if _global_registry() is not None:
        return True
    return False


def is_indexed(root: Path) -> bool:
    """True iff ``root/.gitnexus/`` exists (project has a built index)."""
    return _project_index_dir(root).is_dir()


def status(root: Path) -> dict:
    """Summary dict — version, install state, project-index state."""
    out: dict = {
        "binary": binary_path(),
        "global_registry": str(_global_registry()) if _global_registry() else None,
        "project_indexed": is_indexed(root),
        "project_index_dir": str(_project_index_dir(root)) if is_indexed(root) else None,
        "version": _probe_version(),
    }
    return out


def _probe_version() -> Optional[str]:
    bin_ = binary_path()
    if not bin_:
        return None
    try:
        cp = subprocess.run(
            [bin_, "--version"],
            capture_output=True, text=True, timeout=3,
        )
        if cp.returncode != 0:
            return None
        return cp.stdout.strip() or cp.stderr.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------------------
# Reindex spawn (Stop-hook side)
# ---------------------------------------------------------------------------

def _acquire_lock(root: Path, min_age_seconds: int) -> bool:
    lock = root / _LOCK_FILENAME
    now = time.time()
    if lock.exists():
        try:
            if now - lock.stat().st_mtime < min_age_seconds:
                return False
        except OSError:
            pass
    try:
        lock.write_text(str(int(now)), encoding="utf-8")
        return True
    except OSError:
        return False


def _spawn_analyze(binary: str, root: Path) -> None:
    """Detached ``gitnexus analyze`` for incremental update."""
    try:
        subprocess.Popen(
            [binary, "analyze"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("gitnexus: spawned analyze in %s", root)
    except OSError as e:
        log.debug("could not spawn gitnexus analyze: %s", e)


def reindex_if_dirty_async(
    *,
    cwd: str,
    turn_modified: bool,
    lock_min_age_seconds: int = _DEFAULT_LOCK_MIN_AGE_SECONDS,
) -> None:
    """Detached gitnexus reindex when the turn touched source files.

    Silent no-op when gitnexus is missing or the project isn't indexed.
    """
    try:
        if not turn_modified:
            return
        bin_ = binary_path()
        if not bin_:
            return
        if not cwd:
            return
        root = Path(cwd).resolve()
        marker_root = _find_marker_root(root)
        if marker_root is None:
            return
        if not is_indexed(marker_root):
            return
        if not _acquire_lock(marker_root, lock_min_age_seconds):
            return
        _spawn_analyze(bin_, marker_root)
    except Exception as e:
        log.debug("gitnexus reindex_if_dirty_async failed: %s", e)


def _find_marker_root(start: Path) -> Optional[Path]:
    p = start.resolve()
    while True:
        if (p / ".gitnexus").is_dir() or (p / ".git").exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


# ---------------------------------------------------------------------------
# SessionStart hint
# ---------------------------------------------------------------------------

_HINT_PREFIX = (
    "_gitnexus is indexed for this repo. For richer queries "
    "(impact, context, cypher, hybrid search), prefer the "
    "`mcp__gitnexus__*` tools when available._"
)


def session_start_hint(
    root: Path,
    *,
    show_init_hint: bool = True,
    enabled: bool = True,
) -> Optional[str]:
    """One-line hint, or None when gitnexus isn't relevant here.

    Parameters mirror :func:`axon_integration.session_start_hint`:

    - ``show_init_hint=False`` suppresses the "Run `gitnexus init` ..."
      nag for projects where the binary is installed but no
      ``.gitnexus/`` index exists. The positive "indexed for this repo"
      hint is unaffected.
    - ``enabled=False`` suppresses all hints — used by the dispatcher
      to skip projects where the user hasn't opted in via per-project
      ``mcpServers`` in ``~/.claude.json``.
    """
    if not enabled:
        return None
    if is_indexed(root):
        return _HINT_PREFIX
    if is_available() and show_init_hint:
        return (
            "_gitnexus is installed. Run `gitnexus init` in this repo "
            "to enable richer code-graph queries via its MCP tools._"
        )
    return None
