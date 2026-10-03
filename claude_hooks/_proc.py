"""Process facts that several subsystems need, with no heavy imports.

``process_start_time`` lived in ``claude_hooks/lsp_engine/daemon.py``;
the mailbox needs it on every hook turn, and importing the engine for it
cost ~40 ms per hook.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def process_start_time(pid: int) -> Optional[float]:
    """When ``pid`` actually started, as a unix timestamp.

    Linux-only; None elsewhere, and callers must treat None as "cannot
    tell" rather than as an answer.

    NOT ``Path(f"/proc/{pid}").stat().st_mtime``. That looks like the
    start time and is not: the kernel updates the directory's mtime
    afterwards, so a daemon started 2026-09-16 19:40:20 reported
    02:03:30 the following morning. Two things were built on that
    mistake within an hour of each other — the deploy's staleness check
    (which then *under*-reports, the dangerous direction) and the PID
    reuse guard (which concluded a live daemon's own lock belonged to
    someone else, and so hid a genuinely wedged daemon from ``lsp
    list``).

    Field 22 of ``/proc/<pid>/stat`` is the start time in clock ticks
    since boot; ``btime`` in ``/proc/stat`` is when boot was. The comm
    field is parenthesised and may contain spaces or ``)``, so the
    fields are counted from the LAST ``)``.

    On Windows the same fact comes from ``GetProcessTimes``, whose
    creation time is a FILETIME — 100 ns ticks since 1601-01-01. It is
    worth having there for more than parity: with no ``/proc/<pid>/
    cmdline`` to read, a start time that matches the one recorded in
    the lock file is the only proof available that a pid is still the
    daemon that wrote it, and without some such proof nothing on
    Windows may be signalled at all.
    """
    if pid is None or pid <= 0:
        return None
    if os.name == "nt":
        return _process_start_time_windows(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        ticks = float(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None
    try:
        for line in Path("/proc/stat").read_text(encoding="ascii").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1]) + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, AttributeError):
        return None
    return None


#: Seconds between the FILETIME epoch (1601-01-01) and the unix epoch.
_FILETIME_EPOCH_DELTA = 11644473600.0


def _process_start_time_windows(pid: int) -> Optional[float]:  # pragma: no cover — Windows
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 1e7 - _FILETIME_EPOCH_DELTA
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)
