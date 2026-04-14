"""
claudemem auto-reindex helpers.

claudemem (https://github.com/MadAppGang/claudemem) is a semantic code
search tool whose index must be kept in sync with the project files.
Upstream ships:

- ``claudemem hooks install`` — a git post-commit hook that reindexes
  after each commit. Installed at ``.git/hooks/post-commit``.
- ``claudemem watch`` — a daemon with a filesystem watcher.

Neither covers the gap of **mid-session edits that haven't been
committed yet**. This module provides two helpers for claude-hooks to
bridge that gap:

1. :func:`reindex_if_dirty_async` — fires at end of assistant turn
   (Stop hook). If any Edit/Write/MultiEdit ran this turn, spawn
   ``claudemem index --quiet`` as a detached background process so the
   user doesn't see per-turn latency.

2. :func:`reindex_if_stale_async` — fires on SessionStart. If the
   index mtime trails the newest source-file mtime by more than
   ``staleness_minutes``, kick off a detached reindex. Catches changes
   made outside Claude Code (manual edits, git pulls, branch switches).

Both helpers are **silent no-ops** when:

- ``claudemem`` binary is not on PATH
- project is not a git repo or has no .claudemem/ dir
- ``claudemem index`` is already running for this project
- background spawn fails for any reason

Design principles:

- Never block the hook. The detached ``Popen`` returns immediately.
- Never raise — all failure modes swallowed, logged at debug level.
- Never re-index more than once per minute per project (cheap lock
  file guard avoids pileups when the Stop hook fires rapidly).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.claudemem_reindex")


_LOCK_FILENAME = ".claudemem-reindex.lock"
_LOCK_MIN_AGE_SECONDS = 60  # don't re-reindex within this window


def _find_claudemem() -> Optional[str]:
    return shutil.which("claudemem")


def _project_root(cwd: str) -> Optional[Path]:
    """Walk up from cwd looking for a .git directory. Returns None if missing."""
    if not cwd:
        return None
    p = Path(cwd).resolve()
    while True:
        if (p / ".git").exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


def _claudemem_indexed(root: Path) -> bool:
    """True iff this project has been indexed at least once."""
    return (root / ".claudemem").is_dir()


def _acquire_lock(root: Path) -> bool:
    """Return True if we should proceed (stale or missing lock), False otherwise."""
    lock = root / _LOCK_FILENAME
    now = time.time()
    if lock.exists():
        try:
            age = now - lock.stat().st_mtime
            if age < _LOCK_MIN_AGE_SECONDS:
                log.debug("reindex lock fresh (%ds old) — skipping", int(age))
                return False
        except OSError:
            pass
    try:
        lock.write_text(str(int(now)), encoding="utf-8")
    except OSError as e:
        log.debug("could not write reindex lock: %s", e)
        return False
    return True


def _spawn_reindex(binary: str, root: Path) -> None:
    """Start ``claudemem index --quiet`` as a detached background process."""
    try:
        # Detach: new session, redirect stdio to devnull so parent doesn't block.
        subprocess.Popen(
            [binary, "index", "--quiet"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("spawned claudemem index --quiet in %s", root)
    except OSError as e:
        log.debug("could not spawn claudemem: %s", e)


def reindex_if_dirty_async(
    *,
    cwd: str,
    turn_modified: bool,
) -> None:
    """Spawn a reindex if the turn touched source files. Never raises."""
    try:
        if not turn_modified:
            return
        binary = _find_claudemem()
        if not binary:
            log.debug("claudemem not on PATH — skip")
            return
        root = _project_root(cwd)
        if not root:
            log.debug("cwd %r is not inside a git repo — skip", cwd)
            return
        if not _claudemem_indexed(root):
            log.debug("project %s has no .claudemem — skip", root)
            return
        if not _acquire_lock(root):
            return
        _spawn_reindex(binary, root)
    except Exception as e:
        log.debug("reindex_if_dirty_async failed: %s", e)


def reindex_if_stale_async(
    *,
    cwd: str,
    staleness_minutes: int = 10,
    max_files_to_scan: int = 2000,
) -> None:
    """Spawn a reindex if the index trails the newest source mtime. Never raises."""
    try:
        binary = _find_claudemem()
        if not binary:
            return
        root = _project_root(cwd)
        if not root:
            return
        claudemem_dir = root / ".claudemem"
        if not claudemem_dir.is_dir():
            return

        try:
            index_mtime = max(
                p.stat().st_mtime for p in claudemem_dir.rglob("*") if p.is_file()
            )
        except (OSError, ValueError):
            return

        threshold = index_mtime + staleness_minutes * 60
        now = time.time()

        # If the threshold is in the future (index updated < staleness window
        # ago), we don't need to do anything regardless of newer source files.
        if threshold > now:
            return

        # Cheap mtime scan. Skip hidden dirs, large/binary extensions,
        # and the index dir itself. Only needs to find ONE file newer
        # than the index to trigger a reindex, so bail early.
        ignored_dirs = {".git", ".claudemem", "node_modules", "__pycache__",
                        ".venv", ".cache", ".caliber"}
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
            for fn in filenames:
                count += 1
                if count > max_files_to_scan:
                    return
                try:
                    mt = (Path(dirpath) / fn).stat().st_mtime
                except OSError:
                    continue
                if mt > index_mtime:
                    if _acquire_lock(root):
                        _spawn_reindex(binary, root)
                    return
    except Exception as e:
        log.debug("reindex_if_stale_async failed: %s", e)
