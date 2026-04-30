"""Per-file session affinity lock manager.

Implements Decision 5 of ``docs/PLAN-lsp-engine.md``: when two Claude
Code sessions share a per-project daemon and edit the same file, the
LSP only keeps the last write. Without coordination, session A's
next query would see session B's clobber.

The manager keeps, per file URI:

- ``owner``: session that holds the lock
- ``expires_at``: monotonic deadline; auto-release when no further
  activity in ``debounce_seconds`` (default 30)
- ``queue``: pending ``QueuedChange`` entries from non-owners,
  applied in arrival order when the lock releases

Public methods are deliberately *boring* state-machine ops; the
caller (the daemon) is responsible for forwarding LSP requests,
applying drained changes, and dispatching diagnostics. Keeping the
manager stupid makes it unit-testable without spawning anything.

Concurrency model: one ``threading.Condition`` guards all state.
Public methods are thread-safe. ``query()`` polls in 50 ms slices so
expiries are picked up promptly even without the daemon's sweeper
thread firing.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, NamedTuple, Optional

log = logging.getLogger("claude_hooks.lsp_engine.locks")


class QueuedChange(NamedTuple):
    session: str
    content: str


# Smaller than the default query timeout (500 ms) so a query waiting
# on a foreign owner picks up an expiry that happens *during* the wait
# instead of having to wait for the daemon's sweeper thread.
_QUERY_POLL_INTERVAL_S = 0.050


class SessionLockManager:
    """Per-file affinity lock state machine.

    Drained changes from expired locks are *returned* to the caller,
    not applied here — the manager has no knowledge of LSPs. That
    keeps the contract: every public mutation method returns the work
    the caller needs to do as a result.
    """

    def __init__(
        self,
        debounce_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._debounce = float(debounce_seconds)
        self._clock = clock
        self._cond = threading.Condition()

        self._owners: dict[str, str] = {}
        self._expires: dict[str, float] = {}
        self._queues: dict[str, list[QueuedChange]] = {}

    # ─── public API ──────────────────────────────────────────────────

    def did_change(
        self,
        session: str,
        uri: str,
        content: str,
    ) -> tuple[bool, list[tuple[str, QueuedChange]]]:
        """Record a content change.

        Returns ``(forward_now, drained)``:

        - ``forward_now``: True if the caller should forward
          ``content`` to the LSP for ``uri`` (caller is the new or
          existing owner). False if the change was queued behind a
          foreign owner — the caller does NOT forward it; the
          manager will surface it via a later return value (from
          another caller's drain or from ``tick()``).
        - ``drained``: ``(uri, QueuedChange)`` pairs that became
          eligible during this call (because some lock expired) and
          that the caller should apply to the LSP *before* the
          ``forward_now`` content. The caller applies these in order.
        """
        with self._cond:
            drained = self._drain_expired_locked(self._clock())
            owner = self._owners.get(uri)
            now = self._clock()
            if owner is None or owner == session:
                self._owners[uri] = session
                self._expires[uri] = now + self._debounce
                self._cond.notify_all()
                return True, drained
            # Another session holds the lock — queue.
            self._queues.setdefault(uri, []).append(QueuedChange(session, content))
            return False, drained

    def query(
        self,
        session: str,
        uri: str,
        timeout_ms: int = 500,
    ) -> tuple[bool, list[tuple[str, QueuedChange]]]:
        """Decide whether the caller can forward a read-only query.

        Returns ``(can_forward, drained)``:

        - ``can_forward`` True when the caller is the owner, no one
          owns the URI, or the lock released within ``timeout_ms``.
          False on timeout — the caller decides whether to forward
          anyway with a stale flag (Decision 5 says yes; the LSP's
          current state is the *owner's* view, which is fine for a
          non-owner that hasn't yet edited).
        - ``drained`` same semantics as ``did_change``.
        """
        deadline = self._clock() + (timeout_ms / 1000.0)
        accumulated: list[tuple[str, QueuedChange]] = []
        with self._cond:
            while True:
                drained = self._drain_expired_locked(self._clock())
                accumulated.extend(drained)
                owner = self._owners.get(uri)
                if owner is None or owner == session:
                    return True, accumulated
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False, accumulated
                self._cond.wait(timeout=min(remaining, _QUERY_POLL_INTERVAL_S))

    def owner_of(self, uri: str) -> Optional[str]:
        """Read-only owner check. Drains expired locks first so a
        stale entry doesn't lie. Drained items are *not* returned —
        callers that need them should use ``tick()`` instead."""
        with self._cond:
            self._drain_expired_locked(self._clock())
            return self._owners.get(uri)

    def release_session(self, session: str) -> list[tuple[str, QueuedChange]]:
        """Release every lock owned by ``session`` (called on detach
        / disconnect). Returns ``(uri, QueuedChange)`` pairs the
        caller should apply to the LSP — the next queued change for
        each released URI, if any.
        """
        applied: list[tuple[str, QueuedChange]] = []
        with self._cond:
            for uri in list(self._owners):
                if self._owners[uri] != session:
                    continue
                handoff = self._take_next_locked(uri)
                if handoff is not None:
                    applied.append((uri, handoff))
            self._cond.notify_all()
        return applied

    def tick(
        self,
        now: Optional[float] = None,
    ) -> list[tuple[str, QueuedChange]]:
        """Drain any expired locks. Daemon sweeper calls this every
        ~1 s; callers may also invoke it directly for deterministic
        unit tests with a fake clock.
        """
        ts = now if now is not None else self._clock()
        with self._cond:
            applied = self._drain_expired_locked(ts)
            if applied:
                self._cond.notify_all()
            return applied

    # ─── introspection (tests + daemon status) ───────────────────────

    def queue_length(self, uri: str) -> int:
        with self._cond:
            return len(self._queues.get(uri) or [])

    def held_uris(self) -> list[str]:
        with self._cond:
            self._drain_expired_locked(self._clock())
            return list(self._owners.keys())

    # ─── internals (caller holds self._cond) ─────────────────────────

    def _drain_expired_locked(
        self,
        now: float,
    ) -> list[tuple[str, QueuedChange]]:
        applied: list[tuple[str, QueuedChange]] = []
        for uri in list(self._owners):
            if self._expires.get(uri, float("inf")) > now:
                continue
            handoff = self._take_next_locked(uri)
            if handoff is not None:
                applied.append((uri, handoff))
        return applied

    def _take_next_locked(self, uri: str) -> Optional[QueuedChange]:
        """Pop the next queued change for ``uri`` and transfer
        ownership to its session. Clears all state for ``uri`` if
        the queue is empty.
        """
        queue = self._queues.get(uri) or []
        if not queue:
            self._owners.pop(uri, None)
            self._expires.pop(uri, None)
            return None
        head = queue.pop(0)
        if not queue:
            self._queues.pop(uri, None)
        self._owners[uri] = head.session
        self._expires[uri] = self._clock() + self._debounce
        return head
