"""Directory watchers for the cloud-session relay.

The relay must cost nothing while nobody uses it, so it never polls when
the OS can tell it what changed:

* **Linux** — ``inotify`` through ``ctypes``. While idle the thread is
  blocked in ``select()`` on the inotify descriptor: no timer, no wakeup,
  no disk access. Writes that arrive over Samba are seen too, because
  ``smbd`` writes the file on this host's own filesystem.
* **Windows** — ``ReadDirectoryChangesW`` with overlapped I/O, blocked in
  ``WaitForMultipleObjects`` the same way.
* **Anything else** (or a watcher that fails to start) — a poll of the
  request semaphores' mtimes, no more often than the relay's interval.

Every watcher has the same shape: :meth:`wait` blocks for at most
``timeout`` seconds (``None`` = until something happens) and returns
the set of changed paths, relative to the root, as tuples of parts.
:data:`RESCAN` in the set means "events were lost; look at everything".
:meth:`wake` interrupts a wait from another thread.

Stdlib only, like the rest of the core.
"""
from __future__ import annotations

import ctypes
import logging
import os
import select
import struct
import sys
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.mailbox.watch")

#: Sentinel in a change set: the watcher lost events (queue overflow,
#: a directory it could not watch) and the caller must rescan.
RESCAN = ("*",)

Change = tuple  # tuple[str, ...], parts relative to the root


class Watcher:
    kind = "base"

    def wait(self, timeout: Optional[float]) -> set:
        raise NotImplementedError

    def wake(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ─── Linux: inotify ─────────────────────────────────────────────────────

IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000

#: Directories that only hold structure: we want to hear about new
#: subdirectories, nothing else.
_DIR_MASK = IN_CREATE | IN_MOVED_TO | IN_DELETE_SELF | IN_MOVE_SELF
#: ``requests/`` directories: a file finished being written, or was
#: renamed into place. Not IN_MODIFY — that fires per write() call, and
#: the semaphore protocol makes a half-written file uninteresting anyway.
_FILE_MASK = IN_CLOSE_WRITE | IN_MOVED_TO | IN_DELETE_SELF | IN_MOVE_SELF

_EVENT_HEAD = struct.Struct("iIII")


def _libc():
    import ctypes.util
    name = ctypes.util.find_library("c") or "libc.so.6"
    lib = ctypes.CDLL(name, use_errno=True)
    lib.inotify_init1.argtypes = [ctypes.c_int]
    lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p,
                                      ctypes.c_uint32]
    lib.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
    return lib


def inotify_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        return hasattr(_libc(), "inotify_init1")
    except OSError:
        return False


class InotifyWatcher(Watcher):
    """Watches ``root``, ``root/sessions``, every ``sessions/<alias>`` and
    every ``sessions/<alias>/requests`` — the only places a remote session
    writes. Replies, the inbox summary and ``rejected/`` are written by
    the daemon and deliberately not watched, so the relay does not wake
    itself up."""

    kind = "inotify"

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lib = _libc()
        fd = self._lib.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"inotify_init1: {os.strerror(err)}")
        self._fd = fd
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        self._wd: dict[int, tuple] = {}
        self._watch_tree()

    # -- watch management
    def _add(self, rel: tuple) -> bool:
        path = self.root.joinpath(*rel)
        mask = _FILE_MASK if rel and rel[-1] == "requests" else _DIR_MASK
        wd = self._lib.inotify_add_watch(
            self._fd, os.fsencode(str(path)), mask)
        if wd < 0:
            log.debug("relay: cannot watch %s: %s", path,
                      os.strerror(ctypes.get_errno()))
            return False
        self._wd[wd] = rel
        return True

    def _watch_tree(self) -> None:
        self._add(())
        sessions = self.root / "sessions"
        if sessions.is_dir():
            self._watch_sessions()

    def _watch_sessions(self) -> None:
        if not self._add(("sessions",)):
            return
        try:
            aliases = [p for p in (self.root / "sessions").iterdir()
                       if p.is_dir()]
        except OSError:
            return
        for alias_dir in aliases:
            self._watch_alias(alias_dir.name)

    def _watch_alias(self, alias: str) -> None:
        if not self._add(("sessions", alias)):
            return
        if (self.root / "sessions" / alias / "requests").is_dir():
            self._add(("sessions", alias, "requests"))

    # -- the Watcher interface
    def wait(self, timeout: Optional[float]) -> set:
        ready, _, _ = select.select([self._fd, self._wake_r], [], [],
                                    timeout)
        changes: set = set()
        if self._wake_r in ready:
            try:
                while os.read(self._wake_r, 64):
                    pass
            except BlockingIOError:
                pass
        if self._fd in ready:
            self._drain(changes)
        return changes

    def _drain(self, changes: set) -> None:
        while True:
            try:
                buf = os.read(self._fd, 64 * 1024)
            except BlockingIOError:
                return
            if not buf:
                return
            off = 0
            while off + _EVENT_HEAD.size <= len(buf):
                wd, mask, _cookie, length = _EVENT_HEAD.unpack_from(buf, off)
                raw = buf[off + _EVENT_HEAD.size:off + _EVENT_HEAD.size
                          + length]
                off += _EVENT_HEAD.size + length
                name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
                self._event(wd, mask, name, changes)

    def _event(self, wd: int, mask: int, name: str, changes: set) -> None:
        if mask & IN_Q_OVERFLOW:
            changes.add(RESCAN)
            return
        rel = self._wd.get(wd)
        if mask & IN_IGNORED:
            self._wd.pop(wd, None)
            return
        if rel is None:
            return
        if mask & (IN_DELETE_SELF | IN_MOVE_SELF):
            self._wd.pop(wd, None)
            if rel == ():
                changes.add(RESCAN)
            return
        if not name:
            return
        child = rel + (name,)
        if mask & IN_ISDIR:
            # New structure: watch it, and report it so the caller scans
            # what may already be inside (``mkdir -p`` then a write can
            # beat the watch being added).
            if rel == () and name == "sessions":
                self._watch_sessions()
                changes.add(RESCAN)
            elif rel == ("sessions",):
                self._watch_alias(name)
                changes.add(child)
            elif len(rel) == 2 and rel[0] == "sessions" and name == "requests":
                self._add(child)
                changes.add(child)
            return
        if len(rel) == 3 and rel[-1] == "requests":
            changes.add(child)

    def wake(self) -> None:
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def close(self) -> None:
        for fd in (self._fd, self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass


# ─── Windows: ReadDirectoryChangesW ─────────────────────────────────────

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_NOTIFY_CHANGE_FILE_NAME = 0x1
FILE_NOTIFY_CHANGE_DIR_NAME = 0x2
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
INFINITE = 0xFFFFFFFF
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", ctypes.c_uint32),
                ("OffsetHigh", ctypes.c_uint32),
                ("hEvent", ctypes.c_void_p)]


def parse_notify_buffer(buf: bytes) -> list[str]:
    """``FILE_NOTIFY_INFORMATION`` records → relative names. Pure, so it
    is tested on every platform."""
    names, off = [], 0
    while off + 12 <= len(buf):
        nxt, _action, length = struct.unpack_from("<III", buf, off)
        raw = buf[off + 12:off + 12 + length]
        names.append(raw.decode("utf-16-le", "replace"))
        if not nxt:
            break
        off += nxt
    return names


def classify_relative(name: str) -> Optional[tuple]:
    """A changed path relative to the root → the change the relay cares
    about, or None. Shared by the Windows and poll watchers."""
    parts = tuple(p for p in name.replace("\\", "/").split("/") if p)
    if len(parts) == 4 and parts[0] == "sessions" and parts[2] == "requests":
        return parts[:4]
    if len(parts) in (2, 3) and parts[0] == "sessions":
        if len(parts) == 2 or parts[2] == "requests":
            return parts
    if parts == ("sessions",):
        return RESCAN
    return None


class WindowsWatcher(Watcher):
    """One subtree watch on the root. It also sees the daemon's own
    writes (replies, the inbox summary); those are dropped by
    :func:`classify_relative` before they cause any work."""

    kind = "windows"

    def __init__(self, root: Path):
        self.root = Path(root)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self._k32 = k32
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateEventW.restype = ctypes.c_void_p
        h = k32.CreateFileW(
            str(self.root), FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None,
            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED,
            None)
        if not h or h == _INVALID_HANDLE:
            raise OSError(ctypes.get_last_error(),
                          f"CreateFileW({self.root}) failed")
        self._dir = h
        self._event = k32.CreateEventW(None, True, False, None)
        self._wake_event = k32.CreateEventW(None, False, False, None)
        self._buf = ctypes.create_string_buffer(64 * 1024)
        self._ov = _OVERLAPPED()
        self._ov.hEvent = self._event
        self._pending = False

    def _arm(self) -> None:
        if self._pending:
            return
        self._k32.ResetEvent(ctypes.c_void_p(self._event))
        ok = self._k32.ReadDirectoryChangesW(
            ctypes.c_void_p(self._dir), self._buf, len(self._buf), True,
            FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME
            | FILE_NOTIFY_CHANGE_LAST_WRITE,
            None, ctypes.byref(self._ov), None)
        if not ok:
            raise OSError(ctypes.get_last_error(),
                          "ReadDirectoryChangesW failed")
        self._pending = True

    def wait(self, timeout: Optional[float]) -> set:
        self._arm()
        handles = (ctypes.c_void_p * 2)(self._event, self._wake_event)
        ms = INFINITE if timeout is None else max(0, int(timeout * 1000))
        rc = self._k32.WaitForMultipleObjects(2, handles, False, ms)
        changes: set = set()
        if rc != WAIT_OBJECT_0:
            return changes
        got = ctypes.c_uint32(0)
        self._pending = False
        if not self._k32.GetOverlappedResult(
                ctypes.c_void_p(self._dir), ctypes.byref(self._ov),
                ctypes.byref(got), False):
            changes.add(RESCAN)
            return changes
        if got.value == 0:
            # Buffer overflow: the kernel dropped the records.
            changes.add(RESCAN)
            return changes
        for name in parse_notify_buffer(self._buf.raw[:got.value]):
            change = classify_relative(name)
            if change is not None:
                changes.add(change)
        return changes

    def wake(self) -> None:
        self._k32.SetEvent(ctypes.c_void_p(self._wake_event))

    def close(self) -> None:
        k32 = self._k32
        try:
            if self._pending:
                k32.CancelIoEx(ctypes.c_void_p(self._dir),
                               ctypes.byref(self._ov))
            for h in (self._dir, self._event, self._wake_event):
                k32.CloseHandle(ctypes.c_void_p(h))
        except Exception:  # pragma: no cover - best effort
            pass


# ─── fallback: poll ─────────────────────────────────────────────────────

class PollWatcher(Watcher):
    """Compares request-semaphore mtimes every ``interval`` seconds.

    Only for a host where neither native watcher works. It is the one
    watcher that touches the disk while idle, which is why it is the
    fallback and never the default."""

    kind = "poll"

    def __init__(self, root: Path, interval: float):
        self.root = Path(root)
        self.interval = max(1.0, float(interval))
        self._wake = threading.Event()
        self._seen = self._snapshot()

    def _snapshot(self) -> dict:
        out: dict = {}
        sessions = self.root / "sessions"
        try:
            alias_dirs = [p for p in sessions.iterdir() if p.is_dir()]
        except OSError:
            return out
        for alias_dir in alias_dirs:
            req = alias_dir / "requests"
            try:
                entries = list(os.scandir(req))
            except OSError:
                continue
            for e in entries:
                if e.name.endswith(".sem"):
                    try:
                        out[("sessions", alias_dir.name, "requests",
                             e.name)] = e.stat().st_mtime_ns
                    except OSError:
                        pass
        return out

    def wait(self, timeout: Optional[float]) -> set:
        delay = self.interval if timeout is None else min(timeout,
                                                          self.interval)
        self._wake.wait(delay)
        self._wake.clear()
        now = self._snapshot()
        changes = {k for k, v in now.items() if self._seen.get(k) != v}
        self._seen = now
        return changes

    def wake(self) -> None:
        self._wake.set()


def make_watcher(root: Path, kind: str = "auto", *,
                 poll_interval: float = 30.0) -> Watcher:
    """The cheapest watcher this host supports, or the one asked for.
    A native watcher that fails to start falls back to polling, with a
    warning — a relay that silently stopped hearing requests would be
    worse than one that polls."""
    kind = (kind or "auto").lower()
    if kind in ("auto", "inotify") and inotify_available():
        try:
            return InotifyWatcher(root)
        except OSError as e:
            log.warning("relay: inotify unavailable (%s); polling", e)
    elif kind in ("auto", "windows") and sys.platform == "win32":
        try:
            return WindowsWatcher(root)
        except OSError as e:
            log.warning("relay: ReadDirectoryChangesW unavailable (%s); "
                        "polling", e)
    elif kind not in ("auto", "poll"):
        log.warning("relay: watcher %r not supported here; polling", kind)
    return PollWatcher(root, poll_interval)


__all__ = ["RESCAN", "InotifyWatcher", "PollWatcher", "WindowsWatcher",
           "Watcher", "classify_relative", "inotify_available",
           "make_watcher", "parse_notify_buffer"]
