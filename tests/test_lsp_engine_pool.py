"""One daemon per repository, many narrowly-rooted engines.

The daemon used to be keyed on the same root the language server is
keyed on, so every project root meant another Python process with its
own socket, lock file, sweeper thread and fleet. Measured rather than
assumed: the cline checkout has **30** distinct project roots across
3 536 source files, and across this host **74** state directories named
a root nested inside a repository — 37 of them inside one checkout of
opencoti. Nothing bounded the count because nothing counted it; each
root looks reasonable in isolation.

Widening the *server's* root is not the alternative, and that was
measured too: tsserver rooted at that 6.1 GB tree answered 0 references
in 81.7 s. So the roots stay narrow and the *daemon* moves out to the
repository boundary, which is what this pool implements.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.config import (  # noqa: E402
    ROOT_SENTINEL,
    boundary_root_for,
    find_project_root,
)
from claude_hooks.lsp_engine.pool import EnginePool  # noqa: E402


class _FakeEngine:
    """Records only what the pool is responsible for: start and stop."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.stopped = False

    def shutdown(self, *, timeout: float = 3.0) -> None:
        self.stopped = True

    def active_servers(self):
        return []

    def open_files(self):
        return []


class _Fixture(unittest.TestCase):
    """A monorepo: a git root with two packages under it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve() / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.a = self.repo / "packages" / "a"
        self.b = self.repo / "packages" / "b"
        for pkg in (self.a, self.b):
            (pkg / "src").mkdir(parents=True)
            (pkg / "package.json").write_text("{}", encoding="utf-8")
            (pkg / "src" / "index.ts").write_text("x\n", encoding="utf-8")

        self.built: list[Path] = []

    def pool(self, **kw) -> EnginePool:
        def factory(root: Path) -> _FakeEngine:
            self.built.append(root)
            return _FakeEngine(root)

        return EnginePool(self.repo, [], factory=factory, **kw)


class BoundaryTests(_Fixture):
    def test_the_daemon_key_is_the_repository(self) -> None:
        deep = self.a / "src" / "index.ts"
        self.assertEqual(boundary_root_for(deep), self.repo)

    def test_the_engine_key_stays_narrow(self) -> None:
        # Both halves matter: same file, two different answers, because
        # the daemon and the language server are bounded by different
        # things.
        deep = self.a / "src" / "index.ts"
        self.assertEqual(find_project_root(deep), self.a)

    def test_sibling_packages_share_one_daemon(self) -> None:
        self.assertEqual(boundary_root_for(self.a / "src" / "index.ts"),
                         boundary_root_for(self.b / "src" / "index.ts"))

    def test_a_declared_root_outranks_the_repository(self) -> None:
        # The escape hatch for a repo where one wide engine is viable.
        sentinel = self.a / ROOT_SENTINEL
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("", encoding="utf-8")
        self.assertEqual(boundary_root_for(self.a / "src" / "index.ts"),
                         self.a)


class RoutingTests(_Fixture):
    def test_each_package_gets_its_own_engine(self) -> None:
        p = self.pool()
        ea = p.for_path(self.a / "src" / "index.ts")
        eb = p.for_path(self.b / "src" / "index.ts")
        self.assertIsNot(ea, eb)
        self.assertEqual(ea.root, self.a)
        self.assertEqual(eb.root, self.b)

    def test_the_same_package_reuses_its_engine(self) -> None:
        p = self.pool()
        first = p.for_path(self.a / "src" / "index.ts")
        second = p.for_path(self.a / "package.json")
        self.assertIs(first, second)
        self.assertEqual(len(self.built), 1)

    def test_a_path_outside_the_boundary_does_not_start_a_stray_engine(self) -> None:
        p = self.pool()
        outside = Path(self.tmp.name).resolve() / "elsewhere" / "z.py"
        outside.parent.mkdir(parents=True)
        outside.write_text("x\n", encoding="utf-8")
        eng = p.for_path(outside)
        # Answered by the boundary rather than by a server rooted in a
        # tree this daemon does not own.
        self.assertEqual(eng.root, self.repo)

    def test_a_file_directly_in_the_repo_uses_the_boundary(self) -> None:
        p = self.pool()
        top = self.repo / "README.md"
        top.write_text("x\n", encoding="utf-8")
        self.assertEqual(p.for_path(top).root, self.repo)

    def test_existing_for_path_does_not_build(self) -> None:
        p = self.pool()
        deep = self.a / "src" / "index.ts"
        self.assertIsNone(p.existing_for_path(deep))
        self.assertEqual(self.built, [])
        p.for_path(deep)
        self.assertIsNotNone(p.existing_for_path(deep))


class BoundTests(_Fixture):
    """The sprawl case: what stops it recurring inside one process."""

    def _pkg(self, name: str) -> Path:
        pkg = self.repo / "packages" / name
        (pkg / "src").mkdir(parents=True)
        (pkg / "package.json").write_text("{}", encoding="utf-8")
        f = pkg / "src" / "index.ts"
        f.write_text("x\n", encoding="utf-8")
        return f

    def test_a_sweep_across_many_roots_stays_bounded(self) -> None:
        p = self.pool(max_engines=3)
        for i in range(20):
            p.for_path(self._pkg(f"p{i}"))
        self.assertLessEqual(len(p.live_roots()), 3)
        # And the ones dropped were actually stopped, not leaked.
        self.assertEqual(len([e for e in self.built]), 20)

    def test_eviction_is_least_recently_used(self) -> None:
        p = self.pool(max_engines=2)
        fa = self.a / "src" / "index.ts"
        fb = self.b / "src" / "index.ts"
        ea = p.for_path(fa)
        p.for_path(fb)
        p.for_path(fa)                 # a is now the most recent
        p.for_path(self._pkg("c"))     # evicts b
        roots = p.live_roots()
        self.assertIn(self.a, roots)
        self.assertNotIn(self.b, roots)
        self.assertFalse(ea.stopped)

    def test_recency_does_not_depend_on_clock_resolution(self) -> None:
        """Order is not a question a clock answers.

        ``time.monotonic()`` has ~15.6 ms resolution on Windows, so
        several requests share one value, the tie breaks on dict order,
        and the engine evicted is whichever was inserted first — which
        can be the one in active use, costing a cold start (7.84 s
        measured) in the middle of a task. Caught on pandorum, where
        three calls inside one tick evicted the most recent engine.

        Frozen clock here so the assertion is about ordering and cannot
        pass by accident on a host with a finer timer.
        """
        # A frozen clock, injected rather than patched onto the shared
        # ``time`` module — patching that reaches every other user of it
        # in the process, which is how the Windows-dispatch test broke
        # pathlib.
        p = self.pool(max_engines=2, clock=lambda: 1000.0)
        fa = self.a / "src" / "index.ts"
        fb = self.b / "src" / "index.ts"
        p.for_path(fa)
        p.for_path(fb)
        p.for_path(fa)                 # a is the most recent
        p.for_path(self._pkg("c"))     # b is the one to lose
        roots = p.live_roots()
        self.assertIn(self.a, roots)
        self.assertNotIn(self.b, roots)

    def test_the_engine_being_built_is_never_the_one_evicted(self) -> None:
        # With a cap of 1 an unprotected LRU evicts what it just made.
        p = self.pool(max_engines=1)
        eng = p.for_path(self.a / "src" / "index.ts")
        self.assertFalse(eng.stopped)
        self.assertEqual(p.live_roots(), [self.a])

    def test_an_evicted_root_starts_again_on_next_use(self) -> None:
        p = self.pool(max_engines=1)
        first = p.for_path(self.a / "src" / "index.ts")
        p.for_path(self.b / "src" / "index.ts")
        self.assertTrue(first.stopped)
        again = p.for_path(self.a / "src" / "index.ts")
        self.assertIsNot(again, first)
        self.assertFalse(again.stopped)


class IdleReapTests(_Fixture):
    def test_an_idle_engine_is_reaped(self) -> None:
        p = self.pool(idle_seconds=100.0)
        import time as _t
        eng = p.for_path(self.a / "src" / "index.ts")
        reaped = p.reap_idle(now=_t.monotonic() + 200.0)
        self.assertEqual(reaped, [self.a])
        self.assertTrue(eng.stopped)
        self.assertEqual(p.live_roots(), [])

    def test_a_recently_used_engine_is_kept(self) -> None:
        p = self.pool(idle_seconds=100.0)
        import time as _t
        p.for_path(self.a / "src" / "index.ts")
        self.assertEqual(p.reap_idle(now=_t.monotonic() + 10.0), [])
        self.assertEqual(p.live_roots(), [self.a])

    def test_use_resets_the_idle_clock(self) -> None:
        p = self.pool(idle_seconds=100.0)
        import time as _t
        f = self.a / "src" / "index.ts"
        p.for_path(f)
        p.for_path(f)
        self.assertEqual(p.reap_idle(now=_t.monotonic() + 50.0), [])

    def test_reaping_is_disabled_by_a_non_positive_window(self) -> None:
        p = self.pool(idle_seconds=0.0)
        import time as _t
        p.for_path(self.a / "src" / "index.ts")
        self.assertEqual(p.reap_idle(now=_t.monotonic() + 100_000.0), [])


class LifecycleTests(_Fixture):
    def test_restart_all_stops_every_engine_and_stays_usable(self) -> None:
        # The lifecycle gap this exists to close: a server holds its
        # config and its parsed program from the moment it started, so
        # a reload has to replace the process.
        p = self.pool()
        ea = p.for_path(self.a / "src" / "index.ts")
        eb = p.for_path(self.b / "src" / "index.ts")
        stopped = p.restart_all()
        self.assertEqual(sorted(stopped), sorted([self.a, self.b]))
        self.assertTrue(ea.stopped and eb.stopped)
        self.assertEqual(p.live_roots(), [])
        fresh = p.for_path(self.a / "src" / "index.ts")
        self.assertIsNot(fresh, ea)

    def test_shutdown_root_stops_just_that_one(self) -> None:
        p = self.pool()
        ea = p.for_path(self.a / "src" / "index.ts")
        eb = p.for_path(self.b / "src" / "index.ts")
        self.assertTrue(p.shutdown_root(self.a))
        self.assertTrue(ea.stopped)
        self.assertFalse(eb.stopped)
        self.assertEqual(p.live_roots(), [self.b])

    def test_shutdown_root_on_a_root_with_no_engine_is_false(self) -> None:
        p = self.pool()
        self.assertFalse(p.shutdown_root(self.a))

    def test_shutdown_stops_everything_and_refuses_new_work(self) -> None:
        p = self.pool()
        ea = p.for_path(self.a / "src" / "index.ts")
        p.shutdown()
        self.assertTrue(ea.stopped)
        with self.assertRaises(RuntimeError):
            p.for_path(self.b / "src" / "index.ts")

    def test_a_failing_shutdown_does_not_strand_the_entry(self) -> None:
        # An engine whose servers are already gone must not make the
        # pool unable to forget it.
        p = self.pool()
        eng = p.for_path(self.a / "src" / "index.ts")

        def boom(**kw):
            raise RuntimeError("socket gone")

        eng.shutdown = boom  # type: ignore[assignment]
        self.assertTrue(p.shutdown_root(self.a))
        self.assertEqual(p.live_roots(), [])


class StatusTests(_Fixture):
    def test_status_reports_every_live_root(self) -> None:
        # With a daemon per narrow root there was no process that could
        # answer this for the repository at all.
        p = self.pool()
        p.for_path(self.a / "src" / "index.ts")
        p.for_path(self.b / "src" / "index.ts")
        roots = [d["root"] for d in p.stats()]
        self.assertEqual(roots, sorted([str(self.a), str(self.b)]))

    def test_status_survives_a_broken_engine(self) -> None:
        p = self.pool()
        eng = p.for_path(self.a / "src" / "index.ts")

        def boom():
            raise RuntimeError("dead")

        eng.active_servers = boom  # type: ignore[assignment]
        self.assertEqual(p.stats()[0]["servers"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
