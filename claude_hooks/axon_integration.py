"""
axon integrator — detect and surface https://github.com/harshkedia177/axon
when the user has it installed, without making it a hard dependency.

axon is the recommended companion code-graph engine for claude-hooks
(see COMPANION_TOOLS.md). Pure-Python install (``pip install axoniq``),
KuzuDB-backed, dedicated dead-code detection, watcher mode.

This module follows the same pattern as ``gitnexus_integration``:
silent no-op when missing; spawn ``axon analyze`` on file edits when
the project is indexed; expose detection helpers.

Note: when axon is wired into Claude Code as an MCP server with
``axon serve --watch``, it auto-rebuilds on file changes itself. Our
Stop-hook spawn is a *belt-and-braces* fallback for users who run the
MCP without ``--watch`` or via the legacy single-session config.
axon's own fingerprint check makes the redundant spawn cheap.

Public API:

    is_available() -> bool
    is_indexed(root: Path) -> bool
    binary_path() -> Optional[str]
    status(root: Path) -> dict
    reindex_if_dirty_async(*, cwd, turn_modified, ...) -> None
    session_start_hint(root: Path) -> Optional[str]
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.axon")

_LOCK_FILENAME = ".axon-reindex.lock"
_DEFAULT_LOCK_MIN_AGE_SECONDS = 60


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def binary_path() -> Optional[str]:
    return shutil.which("axon")


def _global_registry() -> Optional[Path]:
    p = Path.home() / ".axon" / "repos"
    return p if p.exists() else None


def _project_index_dir(root: Path) -> Path:
    return root / ".axon"


def is_available() -> bool:
    if binary_path() is not None:
        return True
    if _global_registry() is not None:
        return True
    return False


def is_indexed(root: Path) -> bool:
    return _project_index_dir(root).is_dir()


def status(root: Path) -> dict:
    return {
        "binary": binary_path(),
        "global_registry": str(_global_registry()) if _global_registry() else None,
        "project_indexed": is_indexed(root),
        "project_index_dir": str(_project_index_dir(root)) if is_indexed(root) else None,
        "version": _probe_version(),
    }


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
# Reindex spawn
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
    """Detached ``axon analyze .`` for incremental update."""
    try:
        subprocess.Popen(
            [binary, "analyze", "."],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("axon: spawned analyze in %s", root)
    except OSError as e:
        log.debug("could not spawn axon: %s", e)


def reindex_if_dirty_async(
    *,
    cwd: str,
    turn_modified: bool,
    lock_min_age_seconds: int = _DEFAULT_LOCK_MIN_AGE_SECONDS,
) -> None:
    """Detached ``axon analyze`` when the turn touched source files.

    Silent no-op when axon is missing or the project hasn't been
    initialised by axon (no ``.axon/`` in repo root).
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
        log.debug("axon reindex_if_dirty_async failed: %s", e)


def _find_marker_root(start: Path) -> Optional[Path]:
    p = start.resolve()
    while True:
        if (p / ".axon").is_dir() or (p / ".git").exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


# ---------------------------------------------------------------------------
# SessionStart hint
# ---------------------------------------------------------------------------

_HINT_INDEXED = (
    "_**axon** is indexed for this repo — for richer queries "
    "(`impact`, `context`, `dead_code`, `cypher`, hybrid search), "
    "prefer the `mcp__axon__*` tools when available._"
)

_HINT_AVAILABLE = (
    "_**axon** is installed. Run `axon analyze .` in this repo to "
    "enable richer code-graph queries via `mcp__axon__*`._"
)


def session_start_hint(root: Path) -> Optional[str]:
    """One-line markdown hint, or None when axon isn't here."""
    if is_indexed(root):
        return _HINT_INDEXED
    if is_available():
        return _HINT_AVAILABLE
    return None
