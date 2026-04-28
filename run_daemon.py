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
any code that resolves relative paths (log files, config), and hands
off to the daemon's ``__main__``.

Cross-platform — works on POSIX too (just call ``python run_daemon.py``)
but only the Windows install path uses it. Linux uses systemd's
``WorkingDirectory=`` and macOS uses launchd's ``WorkingDirectory``,
neither of which has the console-window problem.
"""
from __future__ import annotations

import os
import runpy
import sys


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        os.chdir(here)
    except OSError:
        # Non-fatal — daemon doesn't strictly require cwd to be the repo.
        pass
    runpy.run_module("claude_hooks.daemon", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
