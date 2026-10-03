"""Who a session is: its name, and the Claude Code process behind it.

The mailbox used to name a session after its directory. That is wrong
twice over. A user who runs ``/rename opencoti-mac`` has said what the
session is called, and nothing read it: Claude Code records the name in
the transcript (a ``custom-title`` record, repeated as the transcript
grows) and sends no hook event for it. And two sessions in one directory
got the same name, so the second one took the first one's registration
and its badge counted the first one's mail.

This module answers the three questions identity needs, on every OS:

- :func:`session_name` — the name the user gave the session, if any:
  the last ``custom-title`` in the transcript, or else the last
  ``/rename <name>`` for that session in ``history.jsonl``. The fallback
  covers two measured cases: a session whose transcript does not exist
  yet (Claude Code writes it at the first message), and a ``/rename``
  that Claude Code never persisted (xollama's three, on 2.1.278–280).
- :func:`client_process` — the Claude Code process this code runs under,
  as ``(pid, start time)``. A pid alone is not an identity: pids are
  reused, so the start time is what proves a row's process is still the
  one that wrote it.
- :func:`holder_alive` — whether the session holding a registration is
  still running.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.mailbox.identity")

#: How much of a transcript's tail is read per step, and the most read in
#: all. Claude Code re-appends the title every few records (eight in the
#: last 300 KB of a 5 GB transcript, measured), so the first step almost
#: always finds it; the cap bounds a turn full of large tool output.
_TAIL_STEP = 256 * 1024
_TAIL_MAX = 8 * 1024 * 1024

#: ``history.jsonl`` grows by one line per prompt; a just-started session
#: is near its end.
_HISTORY_TAIL = 2 * 1024 * 1024

#: Start times agree to the clock tick; anything closer than this is the
#: same process.
_START_SLACK_S = 2.0

#: A row written before rows carried a process is judged by ``last_seen``.
LEGACY_LIVE_SECONDS = 2 * 3600

#: ``custom-title`` only. ``agent-name`` mirrors the *generated* title
#: when the user never named the session ("project-tracking-council-chat"
#: in xollama, whose three ``/rename xollama`` were never persisted), so
#: reading it would name a session something nobody chose.
_TITLE_RE = re.compile(
    rb'"type"\s*:\s*"custom-title"\s*,\s*'
    rb'"customTitle"\s*:\s*"((?:[^"\\]|\\.)*)"')
_UNSAFE = re.compile(r"[\s@*,]+")


def clean_name(raw: str) -> str:
    """A session name as a mailbox alias.

    ``@`` separates an alias from its host, ``*`` is broadcast and ``,``
    separates recipients, so none of them can be part of a name.
    Whitespace becomes ``-``.
    """
    return _UNSAFE.sub("-", (raw or "").strip()).strip("-")[:64]


def config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def project_slug(project_dir: str) -> str:
    """Claude Code's directory name for a project's transcripts."""
    return re.sub(r"[^A-Za-z0-9]", "-", project_dir or "")


def transcript_for(session_id: str, project_dir: Optional[str] = None
                   ) -> Optional[Path]:
    """The transcript of ``session_id``: the project's own directory
    first, then any project, since the MCP side may only know the id."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    root = config_dir() / "projects"
    if project_dir:
        p = root / project_slug(project_dir) / f"{session_id}.jsonl"
        if p.is_file():
            return p
    try:
        for p in root.glob(f"*/{session_id}.jsonl"):
            return p
    except OSError:
        pass
    return None


def _title_from_tail(path: Path) -> Optional[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            step = _TAIL_STEP
            while True:
                start = max(0, size - step)
                fh.seek(start)
                chunk = fh.read(size - start)
                found = _TITLE_RE.findall(chunk)
                if found:
                    try:
                        return json.loads(b'"' + found[-1] + b'"')
                    except ValueError:
                        return found[-1].decode("utf-8", "replace")
                if start == 0 or step >= _TAIL_MAX:
                    return None
                step *= 4
    except OSError:
        return None


def _title_from_history(session_id: str) -> Optional[str]:
    path = config_dir() / "history.jsonl"
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - _HISTORY_TAIL))
            lines = fh.read().splitlines()
    except OSError:
        return None
    needle = session_id.encode()
    for line in reversed(lines):
        if needle not in line or b"/rename" not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("sessionId") != session_id:
            continue
        display = str(rec.get("display") or "").strip()
        if display.startswith("/rename "):
            return display[len("/rename "):].strip() or None
    return None


def session_name(session_id: str, *, transcript_path: Optional[str] = None,
                 project_dir: Optional[str] = None) -> Optional[str]:
    """The name the user gave this session, cleaned for addressing, or
    None when it has none. Never raises."""
    if not session_id:
        return None
    try:
        path = Path(transcript_path) if transcript_path else None
        if path is None or not path.is_file():
            path = transcript_for(session_id, project_dir)
        raw = _title_from_tail(path) if path is not None else None
        if not raw:
            raw = _title_from_history(session_id)
        name = clean_name(raw or "")
        return name or None
    except Exception:
        log.debug("mailbox: session name lookup failed", exc_info=True)
        return None


# ─── the Claude Code process ────────────────────────────────────────────

def _is_claude(name: str, cmdline: str = "") -> bool:
    base = os.path.basename(name or "").lower()
    if base in ("claude", "claude.exe"):
        return True
    first = os.path.basename((cmdline.split() or [""])[0]).lower()
    return (first in ("claude", "claude.exe")
            or "@anthropic-ai/claude-code" in cmdline)


def _ancestors_linux(start: int):
    pid, seen = start, set()
    while pid and pid > 1 and pid not in seen and len(seen) < 64:
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                stat = fh.read().decode(errors="replace")
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return
        rp = stat.rfind(")")
        yield pid, stat[stat.find("(") + 1:rp], cmd
        pid = int(stat[rp + 2:].split()[1])


def _ancestors_windows(start: int):  # pragma: no cover — Windows
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID,
                                          entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    pid, seen = start, set()
    while pid and pid in table and pid not in seen and len(seen) < 64:
        seen.add(pid)
        ppid, exe = table[pid]
        yield pid, exe, ""
        pid = ppid


def client_process(start_pid: Optional[int] = None
                   ) -> Optional[tuple[int, float]]:
    """``(pid, start time)`` of the nearest Claude Code ancestor, or None.

    ``CLAUDE_HOOKS_CLIENT_PID`` overrides the walk (``0`` = unknown); the
    hook launcher sets nothing, it is for tests and odd launchers.
    """
    from claude_hooks._proc import process_start_time
    forced = os.environ.get("CLAUDE_HOOKS_CLIENT_PID")
    try:
        if forced is not None:
            pid = int(forced)
            if pid <= 0:
                return None
        else:
            pid = None
            walk = (_ancestors_windows if os.name == "nt"
                    else _ancestors_linux)
            for p, name, cmd in walk(start_pid or os.getppid()):
                if _is_claude(name, cmd):
                    pid = p
                    break
            if pid is None:
                return None
        started = process_start_time(pid)
        return (pid, started) if started is not None else None
    except Exception:
        log.debug("mailbox: client process lookup failed", exc_info=True)
        return None


def same_process(a_pid, a_started, b_pid, b_started) -> bool:
    if not a_pid or not b_pid or a_started is None or b_started is None:
        return False
    return (int(a_pid) == int(b_pid)
            and abs(float(a_started) - float(b_started)) <= _START_SLACK_S)


def process_alive(pid, started) -> bool:
    """Is ``pid`` still the process that started at ``started``?"""
    from claude_hooks._proc import process_start_time
    if not pid or started is None:
        return False
    now = process_start_time(int(pid))
    return now is not None and abs(now - float(started)) <= _START_SLACK_S


def holder_alive(row: dict, *, host: str, now_ts: float) -> bool:
    """Is the session in registry ``row`` still running?

    A row from another host is never judged here: its process is not
    ours to look at, and the suffix rule only ever compares rows on this
    host. A row with a recorded process is alive while that process is;
    an older row without one, while it was seen recently.
    """
    if row.get("host") != host:
        return True
    pid, started = row.get("client_pid"), row.get("client_started")
    if pid and started is not None:
        return process_alive(pid, started)
    seen = row.get("last_seen")
    if seen is None:
        return False
    if isinstance(seen, str):
        from datetime import datetime
        try:
            seen = datetime.fromisoformat(seen.replace("Z", "+00:00"))
        except ValueError:
            return False
    try:
        return now_ts - seen.timestamp() <= LEGACY_LIVE_SECONDS
    except Exception:
        return False
