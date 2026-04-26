"""
CLI entrypoint: ``python3 -m claude_hooks.code_graph build [--root PATH] [--full]``

Run this manually after a big refactor or when the report goes stale.
The SessionStart hook also calls ``build_async`` to do the same thing
in the background; this script is what that detached process executes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from claude_hooks.code_graph.builder import build_graph
from claude_hooks.code_graph.detect import (
    graph_dir,
    is_code_repo,
    is_graph_stale,
    project_root,
)

log = logging.getLogger("claude_hooks.code_graph")

_LOCK_FILENAME = ".code-graph-build.lock"
DEFAULT_LOCK_MIN_AGE_SECONDS = 60


def _acquire_lock(out_dir: Path, min_age_seconds: int) -> bool:
    """True if we should proceed; False if a recent build is still running."""
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = out_dir / _LOCK_FILENAME
    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
            if age < min_age_seconds:
                return False
        except OSError:
            pass
    try:
        lock.write_text(str(int(time.time())), encoding="utf-8")
        return True
    except OSError:
        return False


def _release_lock(out_dir: Path) -> None:
    try:
        (out_dir / _LOCK_FILENAME).unlink()
    except OSError:
        pass


def build_async(
    *,
    cwd: str,
    cooldown_minutes: int = 10,
    min_source_files: int = 5,
    max_files_to_scan: int = 2000,
    lock_min_age_seconds: int = DEFAULT_LOCK_MIN_AGE_SECONDS,
) -> None:
    """Spawn ``python -m claude_hooks.code_graph build`` detached.

    Silent no-op when:
      - cwd is not a git repo
      - repo doesn't look like code (< min_source_files)
      - graph is fresh
      - another build is already running (lock guard)
    Mirrors the design of ``claudemem_reindex.reindex_if_stale_async``.
    """
    try:
        root = project_root(cwd)
        if not root:
            return
        if not is_code_repo(
            root,
            min_source_files=min_source_files,
            max_files_to_scan=max_files_to_scan,
        ):
            return
        if not is_graph_stale(
            root,
            cooldown_minutes=cooldown_minutes,
            max_files_to_scan=max_files_to_scan,
        ):
            return
        out_dir = graph_dir(root)
        if not _acquire_lock(out_dir, lock_min_age_seconds):
            return
        # Detached subprocess — don't await.
        subprocess.Popen(
            [sys.executable, "-m", "claude_hooks.code_graph", "build",
             "--root", str(root)],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": _self_pythonpath()},
        )
        log.info("code_graph: spawned build in %s", root)
    except Exception as e:
        log.debug("build_async failed: %s", e)


def _self_pythonpath() -> str:
    """Make sure the spawned subprocess can import claude_hooks.

    We add the repo root (parent of the package) to PYTHONPATH so
    ``python -m claude_hooks.code_graph`` works even when claude-hooks
    isn't pip-installed (the common case for the dev checkout).
    """
    pkg_root = Path(__file__).resolve().parent.parent.parent
    existing = os.environ.get("PYTHONPATH", "")
    if str(pkg_root) in existing.split(os.pathsep):
        return existing
    return os.pathsep.join([str(pkg_root), existing]) if existing else str(pkg_root)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m claude_hooks.code_graph")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="Build or refresh the graph")
    p_build.add_argument("--root", type=Path, default=Path.cwd(),
                         help="Project root (defaults to cwd)")
    p_build.add_argument("--full", action="store_true",
                         help="Ignore cache, re-parse every file")
    p_build.add_argument("--max-files", type=int, default=20000)
    p_build.add_argument("--quiet", action="store_true")

    p_info = sub.add_parser("info", help="Print stats about the existing graph")
    p_info.add_argument("--root", type=Path, default=Path.cwd())

    args = ap.parse_args(argv)

    if not args.quiet if hasattr(args, "quiet") else True:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    root = project_root(str(args.root))
    if not root:
        print(f"error: {args.root} is not inside a git repo", file=sys.stderr)
        return 2

    if args.cmd == "build":
        out_dir = graph_dir(root)
        # Acquire lock here too so manual runs don't collide with the
        # detached spawn from SessionStart.
        if not _acquire_lock(out_dir, DEFAULT_LOCK_MIN_AGE_SECONDS):
            print("another build is already running (lock fresh) — skipping",
                  file=sys.stderr)
            return 0
        try:
            stats = build_graph(root, max_files=args.max_files,
                                incremental=not args.full)
        finally:
            _release_lock(out_dir)
        if not args.quiet:
            print(json.dumps(stats, indent=2))
        return 0

    if args.cmd == "info":
        from claude_hooks.code_graph.detect import graph_json_path
        gj = graph_json_path(root)
        if not gj.exists():
            print(f"no graph at {gj} — run 'build' first", file=sys.stderr)
            return 1
        try:
            payload = json.loads(gj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"could not read graph: {e}", file=sys.stderr)
            return 1
        print(json.dumps(payload.get("graph", {}), indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
