"""
Cross-process store gate — serialises detached memory stores.

Why this exists
---------------

``hooks.stop.detach_store`` forks a ``claude_hooks.store_async`` child
so the Stop hook doesn't block on embedding. That fixed the dropped-
memory bug, but it replaced a *serialised* workload with a *concurrent*
one, and concurrency broke two things at once (observed live on solidpc,
2026-07-25):

1. **Dedup stopped working.** ``store_async`` runs dedup-recall and then
   store. Two children spawned seconds apart each complete their
   dedup-recall *before* either has written, so neither sees the other
   and both store. The memory table grew exact duplicate pairs.

2. **Interactive recall starved.** Every store costs two embeds. A
   local CPU embedder has finite throughput (~250-330 tok/s on the
   reference Ryzen 5600G, AVX2-only), so N concurrent stores multiply
   the in-flight embed work and ``UserPromptSubmit`` — which is on the
   user's critical path and bounded by a hook timeout — loses its
   budget waiting behind background writes.

Both are fixed by the same thing: let at most **one** detached store be
in flight at a time. Serialising means store N's dedup-recall runs after
store N-1 has committed (so dedup sees it), and background embed load is
capped at one request regardless of how many sessions are active,
leaving the embedder's remaining capacity for interactive recall.

What this is *not*
------------------

This gate is deliberately **only** taken by the store path. Recall must
never block on a store — that would reintroduce the very stall this is
meant to prevent. Recall is latency-critical and read-only; it simply
races ahead while a store waits its turn.

Locking mechanics follow the same POSIX-``flock`` / Windows-
``msvcrt.locking`` split as :mod:`claude_hooks.lsp_engine.daemon`,
including its ``_WIN_LOCK_OFFSET`` lesson: on Windows a byte-range lock
makes those bytes unreadable to *other* processes, so the lock byte sits
far past any payload. Unlike the daemon's lock (non-blocking, used to
detect "already running"), this one **queues**: it polls for the lock
until it wins or the timeout expires.

Failure policy: on timeout, or if the lock can't be used at all
(unwritable ``~/.claude``, missing ``fcntl``/``msvcrt``), the gate
degrades to *open* — the caller proceeds ungated. A possible duplicate
is strictly better than a silently dropped memory, which is the
regression this whole subsystem exists to prevent.
"""

from __future__ import annotations

import errno
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

if os.name == "nt":  # pragma: no cover - platform-split
    try:
        import msvcrt  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        msvcrt = None  # type: ignore[assignment]
    fcntl = None  # type: ignore[assignment]
else:
    msvcrt = None  # type: ignore[assignment]
    try:
        import fcntl  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover
        fcntl = None  # type: ignore[assignment]

log = logging.getLogger("claude_hooks.store_lock")

# Mirror of lsp_engine.daemon._WIN_LOCK_OFFSET. On Windows a byte-range
# lock blocks *reads* of those bytes by other processes, so lock a byte
# well past anything we'd ever write. Windows permits locking beyond
# EOF (a reservation; the file is not grown).
_WIN_LOCK_OFFSET = 4096

# How long a queued store waits for its turn before giving up and
# proceeding ungated. Generous: a store is a background task with no
# hook timeout over it, and the whole point is to wait rather than pile
# on. Sized so a backlog of slow (~30 KB) embeds can still drain.
DEFAULT_TIMEOUT_S = 300.0

# Poll interval while queued. Coarse enough to cost nothing, fine
# enough that a freed gate is claimed promptly.
DEFAULT_POLL_S = 0.25

_ENV_PATH = "CLAUDE_HOOKS_STORE_LOCK_PATH"
_ENV_TIMEOUT = "CLAUDE_HOOKS_STORE_LOCK_TIMEOUT"


def lock_path() -> Optional[Path]:
    """Resolve the gate's lock file, or ``None`` if unusable.

    ``CLAUDE_HOOKS_STORE_LOCK_PATH`` overrides (tests point it at a
    tmpdir so a real run never contends with a test run).
    """
    override = os.environ.get(_ENV_PATH)
    if override:
        return Path(override)
    try:
        d = Path(os.path.expanduser("~")) / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        return d / "claude-hooks-store.lock"
    except OSError as e:  # pragma: no cover - unwritable HOME
        log.debug("store gate unavailable (cannot use ~/.claude): %s", e)
        return None


def _timeout_default() -> float:
    raw = os.environ.get(_ENV_TIMEOUT)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_S


def _try_lock(fd: int) -> bool:
    """One non-blocking acquisition attempt. True if we now hold it."""
    try:
        if os.name == "nt":
            if msvcrt is None:  # pragma: no cover
                return True
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[union-attr]
            os.lseek(fd, 0, os.SEEK_SET)
        else:
            if fcntl is None:  # pragma: no cover
                return True
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[union-attr]
        return True
    except OSError as e:
        # POSIX: EAGAIN/EWOULDBLOCK. Windows: EACCES on a held range.
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN,
                       errno.EACCES, errno.EDEADLK):
            return False
        # Anything else (EBADF, ENOLCK, a filesystem without locking)
        # means the gate can't work here — degrade to open rather than
        # blocking the store forever.
        log.debug("store gate: unusable lock (%s) — proceeding ungated", e)
        return True


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            if msvcrt is None:  # pragma: no cover
                return
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
        else:
            if fcntl is None:  # pragma: no cover
                return
            fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
    except OSError:  # pragma: no cover - defensive
        pass


@contextmanager
def store_gate(timeout: Optional[float] = None,
               poll: float = DEFAULT_POLL_S) -> Iterator[bool]:
    """Serialise the enclosed store against other processes' stores.

    Yields ``True`` when the gate was actually held, ``False`` when the
    caller is proceeding ungated (timeout, or locking unavailable). The
    body runs either way — see the module docstring's failure policy:
    a duplicate beats a dropped memory.

    Usage::

        with store_gate() as held:
            if not held:
                log.warning("store gate timed out; possible duplicate")
            dedup_and_store(...)
    """
    if timeout is None:
        timeout = _timeout_default()

    path = lock_path()
    if path is None:
        yield False
        return

    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        log.debug("store gate: cannot open %s (%s) — proceeding ungated",
                  path, e)
        yield False
        return

    deadline = time.monotonic() + max(0.0, timeout)
    waited_from = time.monotonic()
    held = False
    try:
        while True:
            if _try_lock(fd):
                held = True
                break
            if time.monotonic() >= deadline:
                log.warning(
                    "store gate: waited %.1fs for another store to finish; "
                    "proceeding ungated (a duplicate is possible)",
                    time.monotonic() - waited_from,
                )
                break
            time.sleep(poll)

        if held:
            waited = time.monotonic() - waited_from
            if waited >= poll:
                log.debug("store gate: acquired after %.1fs queued", waited)
            # Record who holds it — purely diagnostic; readers on POSIX
            # are unaffected by flock, and on Windows the payload sits
            # far below the locked byte.
            try:
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()}\n{int(time.time())}\n".encode("ascii"))
            except OSError:  # pragma: no cover - diagnostic only
                pass
        yield held
    finally:
        if held:
            _unlock(fd)
        try:
            os.close(fd)
        except OSError:  # pragma: no cover
            pass
