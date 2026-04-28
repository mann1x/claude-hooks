#!/usr/bin/env python3
"""
Launcher for ``claude_hooks.daemon`` used by the Windows scheduled task.

Why a launcher and not the cmd shim? Task Scheduler launches the daemon
in the user's interactive session. ``cmd.exe`` and ``python.exe`` are
both *console* binaries — Windows allocates a console for the parent
cmd window, and that console stays visible for as long as the daemon
runs. The user sees a permanent black box.

``pythonw.exe`` is the windowless variant of the Python interpreter.
Pointing the scheduled task at it directly (no cmd wrapper) avoids the
console allocation entirely. This file is the script ``pythonw`` runs:
it self-locates the repo, prepends it to ``sys.path`` so the package
imports work without a pip install, sets ``cwd`` to the repo root for
any code that resolves relative paths (log files, config), redirects
stdout/stderr to a rotated log file on Windows (because pythonw has no
console and Task Scheduler doesn't capture stderr), and hands off to
the daemon's ``__main__``.

Cross-platform — works on POSIX too (just call ``python run_daemon.py``)
but only the Windows install path uses it. Linux uses systemd's
``WorkingDirectory=`` plus its own journald capture, and macOS uses
launchd's ``WorkingDirectory`` plus its unified log — neither has the
console-window or vanished-stderr problems.
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


# Rotate the Windows log file once it crosses this many bytes. 5 MiB
# keeps multi-week steady-state operation in scope without blowing up
# disk on a plugin-misbehaving day.
_LOG_ROTATE_MAX_BYTES = 5 * 1024 * 1024


def _rotate_if_large(path: Path, *, max_bytes: int) -> None:
    """Rename ``path`` to ``path.name + ".1"`` when it exceeds
    ``max_bytes``. Best-effort — any failure is silent so a logging
    hiccup never blocks the daemon from starting."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    backup = path.parent / (path.name + ".1")
    try:
        if backup.exists():
            backup.unlink()
    except OSError:
        pass
    try:
        path.rename(backup)
    except OSError:
        pass


def _setup_windows_log_redirect(
    log_path: Path,
    *,
    max_bytes: int = _LOG_ROTATE_MAX_BYTES,
) -> None:
    """On Windows, dup stdout + stderr into ``log_path`` so the daemon
    has somewhere to write when launched via pythonw + Task Scheduler.

    No-op on POSIX — systemd/launchd already capture stderr to their
    native log surface (journald / unified log). Doubling that into a
    file would be churn for no benefit and would compete for write
    ordering with the platform's own logger.

    Best-effort. If the parent dir can't be created or the file can't
    be opened, leaves stdout/stderr alone — the daemon should still
    come up. Logging is a debugging aid, not a hard dependency.
    """
    if os.name != "nt":
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    _rotate_if_large(log_path, max_bytes=max_bytes)
    try:
        # Line-buffered append so a tail picks up entries as they land.
        # Encoding utf-8 matches Python's default logging encoding.
        f = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = f
    sys.stderr = f


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        os.chdir(here)
    except OSError:
        # Non-fatal — daemon doesn't strictly require cwd to be the repo.
        pass

    # The path mirrors what ``daemon_ctl._platform_log_path`` returns
    # on Windows, so ``claude-hooks-daemon-ctl tail`` reads what we
    # write here.
    log_path = (
        Path(os.environ.get("USERPROFILE") or Path.home())
        / ".claude" / "claude-hooks-daemon.log"
    )
    _setup_windows_log_redirect(log_path)

    runpy.run_module("claude_hooks.daemon", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
