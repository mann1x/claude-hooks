#!/usr/bin/env python3
"""
Launcher for ``claude_hooks.proxy`` used by the Windows scheduled task.

Mirror of ``run_daemon.py`` but for the local HTTP proxy. The proxy is
a long-running Python server, so on Windows it would normally pull in
a permanent ``cmd.exe`` console window via the ``.cmd`` shim. Pointing
the scheduled task at ``pythonw.exe`` + this script avoids the console
allocation entirely.

Cross-platform -- the script also works on POSIX (``python run_proxy.py``)
but only the Windows install path uses it. Linux/macOS install via
systemd / launchd, both of which already capture stderr natively.
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


_LOG_ROTATE_MAX_BYTES = 5 * 1024 * 1024


def _rotate_if_large(path: Path, *, max_bytes: int) -> None:
    """Rename ``path`` to ``path.name + ".1"`` when it exceeds
    ``max_bytes``. Best-effort; logging hiccups never block startup."""
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
    """On Windows, dup stdout + stderr into ``log_path`` so the proxy
    has somewhere to write when launched via pythonw + Task Scheduler.

    No-op on POSIX -- systemd/launchd already capture stderr to their
    native log surface."""
    if os.name != "nt":
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    _rotate_if_large(log_path, max_bytes=max_bytes)
    try:
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
        pass

    log_path = (
        Path(os.environ.get("USERPROFILE") or Path.home())
        / ".claude" / "claude-hooks-proxy.log"
    )
    _setup_windows_log_redirect(log_path)

    runpy.run_module("claude_hooks.proxy", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
