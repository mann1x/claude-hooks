"""Unit tests for the session affinity lock manager.

The manager is the thing that makes the shared-daemon decision (Q5 of
``docs/PLAN-lsp-engine.md``) actually work, so it gets its own
focused test file. Pure logic — no LSPs, no sockets, no subprocess.
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Callable, List

from claude_hooks.lsp_engine.locks import (
    QueuedChange,
    SessionLockManager,
)


def _fake_clock() -> tuple[Callable[[], float], Callable[[float], None]]:
    """Return (clock, advance) tuple — the clock starts at 0 and only
    moves when ``advance()`` is called. Lets expiry tests be
    deterministic without sleeping.
    """
    state = [0.0]

    def clock() -> float:
        return state[0]

    def advance(seconds: float) -> None:
        state[0] += seconds

    return clock, advance


class TestBasicAcquire(unittest.TestCase):
    def setUp(self) -> None:
        self.clock, self.advance = _fake_clock()
        self.mgr = SessionLockManager(debounce_seconds=30.0, clock=self.clock)

    def test_first_did_change_takes_lock(self) -> None:
        forward, drained = self.mgr.did_change("A", "/foo.py", "x = 1")
        self.assertTrue(forward)
        self.assertEqual(drained, [])
        self.assertEqual(self.mgr.owner_of("/foo.py"), "A")

    def test_owner_can_extend_lock(self) -> None:
        self.mgr.did_change("A", "/foo.py", "x = 1")
        forward, _ = self.mgr.did_change("A", "/foo.py", "x = 2")
        self.assertTrue(forward)
        # Extension means expiry pushed forward — owner unchanged.
        self.assertEqual(self.mgr.owner_of("/foo.py"), "A")

    def test_foreign_session_change_is_queued(self) -> None:
        self.mgr.did_change("A", "/foo.py", "first")
        forward, drained = self.mgr.did_change("B", "/foo.py", "second")
        self.assertFalse(forward)
        self.assertEqual(drained, [])
        self.assertEqual(self.mgr.queue_length("/foo.py"), 1)
        # Owner is still A; B's change waits.
        self.assertEqual(self.mgr.owner_of("/foo.py"), "A")

    def test_different_files_dont_contend(self) -> None:
        self.mgr.did_change("A", "/foo.py", "x")
        forward, _ = self.mgr.did_change("B", "/bar.py", "y")
        self.assertTrue(forward)
        self.assertEqual(self.mgr.owner_of("/foo.py"), "A")
        self.assertEqual(self.mgr.owner_of("/bar.py"), "B")

    def test_queue_orders_arrivals(self) -> None:
        self.mgr.did_change("A", "/foo.py", "first")
        self.mgr.did_change("B", "/foo.py", "second")
        self.mgr.did_change("C", "/foo.py", "third")
        self.assertEqual(self.mgr.queue_length("/foo.py"), 2)


class TestExpiryAndDrain(unittest.TestCase):
    def setUp(self) -> None:
        self.clock, self.advance = _fake_clock()
        self.mgr = SessionLockManager(debounce_seconds=30.0, clock=self.clock)

    def test_tick_drains_expired_into_queued_session(self) -> None:
        self.mgr.did_change("A", "/foo.py", "first")
        self.mgr.did_change("B", "/foo.py", "second")
        self.advance(31.0)  # past A's debounce
        drained = self.mgr.tick()
        self.assertEqual(len(drained), 1)
        path, change = drained[0]
        self.assertEqual(path, "/foo.py")
        self.assertEqual(change, QueuedChange("B", "second"))
        # B is now the owner with a fresh debounce window.
        self.assertEqual(self.mgr.owner_of("/foo.py"), "B")

    def test_tick_releases_lock_with_empty_queue(self) -> None:
        self.mgr.did_change("A", "/foo.py", "x")
        self.advance(31.0)
        drained = self.mgr.tick()
        # Nothing queued → no handoff to apply, lock fully released.
        self.assertEqual(drained, [])
        self.assertIsNone(self.mgr.owner_of("/foo.py"))

    def test_did_change_drains_inline(self) -> None:
        """An incoming did_change should pick up a stale (expired)
        owner's slot before deciding whether to queue. The test
        verifies that A's lock expires, then B's did_change drains
        A's slot AND takes the lock (no queue needed)."""
        self.mgr.did_change("A", "/foo.py", "first")
        self.advance(31.0)
        forward, drained = self.mgr.did_change("B", "/foo.py", "second")
        self.assertTrue(forward)  # B got the lock cleanly
        self.assertEqual(drained, [])  # A had no queued items behind it
        self.assertEqual(self.mgr.owner_of("/foo.py"), "B")

    def test_owner_extends_past_initial_debounce(self) -> None:
        self.mgr.did_change("A", "/foo.py", "first")
        self.advance(20.0)  # halfway through debounce
        self.mgr.did_change("A", "/foo.py", "second")
        self.advance(20.0)  # would have expired without the extension
        drained = self.mgr.tick()
        self.assertEqual(drained, [])
        # Still A's lock — extension worked.
        self.assertEqual(self.mgr.owner_of("/foo.py"), "A")


class TestSessionRelease(unittest.TestCase):
    def setUp(self) -> None:
        self.clock, self.advance = _fake_clock()
        self.mgr = SessionLockManager(debounce_seconds=30.0, clock=self.clock)

    def test_release_session_drops_locks_and_drains_queues(self) -> None:
        self.mgr.did_change("A", "/foo.py", "first")
        self.mgr.did_change("B", "/foo.py", "second")
        applied = self.mgr.release_session("A")
        self.assertEqual(len(applied), 1)
        path, change = applied[0]
        self.assertEqual(path, "/foo.py")
        self.assertEqual(change, QueuedChange("B", "second"))
        self.assertEqual(self.mgr.owner_of("/foo.py"), "B")

    def test_release_session_with_no_queue_clears_lock(self) -> None:
        self.mgr.did_change("A", "/foo.py", "x")
        applied = self.mgr.release_session("A")
        self.assertEqual(applied, [])
        self.assertIsNone(self.mgr.owner_of("/foo.py"))

    def test_release_session_only_affects_own_locks(self) -> None:
        self.mgr.did_change("A", "/foo.py", "x")
        self.mgr.did_change("B", "/bar.py", "y")
        self.mgr.release_session("A")
        self.assertIsNone(self.mgr.owner_of("/foo.py"))
        self.assertEqual(self.mgr.owner_of("/bar.py"), "B")

    def test_release_session_drains_multiple_files(self) -> None:
        self.mgr.did_change("A", "/foo.py", "f1")
        self.mgr.did_change("A", "/bar.py", "b1")
        self.mgr.did_change("B", "/foo.py", "f2")
        self.mgr.did_change("C", "/bar.py", "b2")
        applied = self.mgr.release_session("A")
        applied_dict = {path: ch for path, ch in applied}
        self.assertEqual(applied_dict["/foo.py"], QueuedChange("B", "f2"))
        self.assertEqual(applied_dict["/bar.py"], QueuedChange("C", "b2"))


class TestQueryWaiting(unittest.TestCase):
    """Query semantics need real time because cond.wait sleeps in
    real wall-clock. Tests stay short (under 200 ms each)."""

    def test_query_by_owner_returns_immediately(self) -> None:
        mgr = SessionLockManager(debounce_seconds=30.0)
        mgr.did_change("A", "/foo.py", "x")
        can_forward, _ = mgr.query("A", "/foo.py", timeout_ms=10)
        self.assertTrue(can_forward)

    def test_query_with_no_owner_returns_immediately(self) -> None:
        mgr = SessionLockManager(debounce_seconds=30.0)
        can_forward, _ = mgr.query("Anyone", "/foo.py", timeout_ms=10)
        self.assertTrue(can_forward)

    def test_query_blocks_then_times_out_under_foreign_owner(self) -> None:
        mgr = SessionLockManager(debounce_seconds=30.0)
        mgr.did_change("A", "/foo.py", "x")
        start = time.monotonic()
        can_forward, _ = mgr.query("B", "/foo.py", timeout_ms=150)
        elapsed = time.monotonic() - start
        self.assertFalse(can_forward)
        # We waited at least ~the timeout, but not 10× it.
        self.assertGreater(elapsed, 0.10)
        self.assertLess(elapsed, 0.50)

    def test_query_unblocks_when_owner_releases(self) -> None:
        mgr = SessionLockManager(debounce_seconds=30.0)
        mgr.did_change("A", "/foo.py", "x")

        result: List[bool] = []

        def query_b():
            can_forward, _ = mgr.query("B", "/foo.py", timeout_ms=2000)
            result.append(can_forward)

        t = threading.Thread(target=query_b)
        t.start()
        # Give the query a moment to enter the wait loop.
        time.sleep(0.10)
        # Releasing A's lock hands off to B (no queue) — query returns true.
        mgr.release_session("A")
        t.join(timeout=1.0)
        self.assertEqual(result, [True])


class TestIntrospection(unittest.TestCase):
    def test_held_uris_lists_only_active_locks(self) -> None:
        clock, advance = _fake_clock()
        mgr = SessionLockManager(debounce_seconds=10.0, clock=clock)
        mgr.did_change("A", "/foo.py", "x")
        mgr.did_change("B", "/bar.py", "y")
        self.assertEqual(set(mgr.held_uris()), {"/foo.py", "/bar.py"})
        advance(11.0)
        # Drain implicit on read — both locks expire, no queued items.
        self.assertEqual(mgr.held_uris(), [])


if __name__ == "__main__":
    unittest.main()
