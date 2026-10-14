"""Collect what councils leave on disk.

Every council writes under ``<cwd>/.claude-hooks/consultants/<sid>/``,
and nothing ever removed any of it: a coder lane's ``coder-out/`` stays
forever, and so does every empty directory a run created and never
used. This module removes the two kinds of leftovers that are safe to
remove without asking:

* **empty directories** inside a session dir — removed when the council
  ends (:func:`prune_empty`);
* **``coder-out/`` trees** whose newest file is older than a TTL —
  removed by the idle reaper's periodic sweep (:func:`sweep_project`)
  and by ``claude-consultants gc``.

It never removes a session dir's records (``metadata.json``, the
transcript, the consultancy file): those are what ``show`` and
``follow-up`` read, so deleting them is a decision for the user, not a
reaper. Any session still in the in-flight index is skipped, so a
running or suspended council keeps its files.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("consultants.server.session_gc")

#: ``coder-out/`` trees untouched for this long are removed.
DEFAULT_CODER_OUT_TTL_DAYS: float = 30.0

#: The reaper sweeps one project at most this often.
SWEEP_INTERVAL_S: float = 3600.0

_last_sweep: dict[str, float] = {}
_sweep_lock = threading.Lock()


def sessions_root(cwd: str | os.PathLike) -> Path:
    return Path(cwd) / ".claude-hooks" / "consultants"


def _newest_mtime(root: Path) -> float:
    newest = root.stat().st_mtime
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                continue
    return newest


def _inflight_sids() -> set[str]:
    try:
        from consultants.server import inflight
        return {str(r.get("sid")) for r in inflight.load() if r.get("sid")}
    except Exception:  # pragma: no cover - defensive
        log.exception("in-flight index unreadable; skipping every session")
        return {"*"}


def prune_empty(session_dir: str | os.PathLike) -> int:
    """Remove empty directories under ``session_dir`` (bottom-up), and
    the session dir itself if nothing is left in it. Returns how many
    directories were removed."""
    root = Path(session_dir)
    if not root.is_dir():
        return 0
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        try:
            os.rmdir(dirpath)       # fails unless empty: that is the test
            removed += 1
        except OSError:
            continue
    return removed


def sweep_project(cwd: str | os.PathLike, *,
                  ttl_days: float = DEFAULT_CODER_OUT_TTL_DAYS,
                  skip_sids: Iterable[str] = (),
                  dry_run: bool = False,
                  now: Optional[float] = None) -> list[dict]:
    """Remove stale ``coder-out/`` trees and empty dirs of one project.

    Returns one record per action (``{"sid", "path", "action", "bytes"}``);
    with ``dry_run`` nothing is removed and the records say what would be.
    """
    root = sessions_root(cwd)
    if not root.is_dir():
        return []
    now = time.time() if now is None else now
    cutoff = now - ttl_days * 86400.0
    skip = set(skip_sids) | _inflight_sids()
    if "*" in skip:
        return []
    actions: list[dict] = []
    for session_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        sid = session_dir.name
        if sid in skip:
            continue
        out = session_dir / "coder-out"
        if out.is_dir() and not out.is_symlink():
            try:
                stale = _newest_mtime(out) < cutoff
            except OSError:
                stale = False
            if stale:
                size = sum(f.stat().st_size for f in out.rglob("*")
                           if f.is_file() and not f.is_symlink())
                actions.append({"sid": sid, "path": str(out),
                                "action": "remove coder-out", "bytes": size})
                if not dry_run:
                    shutil.rmtree(out, ignore_errors=True)
        if dry_run:
            continue
        n = prune_empty(session_dir)
        if n:
            actions.append({"sid": sid, "path": str(session_dir),
                            "action": f"removed {n} empty dir(s)", "bytes": 0})
    return actions


def maybe_sweep(cwds: Iterable[str], *, skip_sids: Iterable[str] = (),
                ttl_days: float = DEFAULT_CODER_OUT_TTL_DAYS,
                interval_s: float = SWEEP_INTERVAL_S,
                now: Optional[float] = None) -> int:
    """The reaper's entry point: sweep each project at most once per
    ``interval_s``. Returns how many actions were taken. Never raises."""
    now = time.time() if now is None else now
    skip = list(skip_sids)
    taken = 0
    for cwd in {str(c) for c in cwds if c}:
        with _sweep_lock:
            if now - _last_sweep.get(cwd, 0.0) < interval_s:
                continue
            _last_sweep[cwd] = now
        try:
            done = sweep_project(cwd, ttl_days=ttl_days, skip_sids=skip,
                                 now=now)
        except Exception:
            log.exception("session gc sweep failed for %s", cwd)
            continue
        for a in done:
            log.info("session gc: %s %s (%s)", a["action"], a["path"], a["sid"])
        taken += len(done)
    return taken


__all__ = [
    "DEFAULT_CODER_OUT_TTL_DAYS", "SWEEP_INTERVAL_S",
    "maybe_sweep", "prune_empty", "sessions_root", "sweep_project",
]
