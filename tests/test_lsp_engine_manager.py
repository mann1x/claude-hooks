"""Supervision for daemons nothing owned.

The lsp_engine daemons are lazy-spawned by whoever needs one and outlive
that process on purpose — a warm language server is the whole point.
What was missing is everything after. Measured on this host before the
manager existed:

    state dirs            : 307
    live daemons          : 175
      orphaned (tree gone): 156   -> 3.37 GB RSS
      with a session      :  14
    dead state dirs       : 132

The 156 are daemons whose project directory had been deleted — test
fixtures under ``/tmp``, scratch checkouts — each still holding a fleet
of language servers for a tree that no longer exists. Nothing listed
them, nothing reaped them, and nothing could reload them.

What is pinned here is mostly what the reaper must *not* do: never reap
a daemon with an attached session, never delete the state of a daemon
that is merely wedged, and never spawn anything while answering a
question about what is running.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine_manager import LspEngineManager  # noqa: E402


class _FakeClient:
    def __init__(self, status: dict, *, reload_raises=False) -> None:
        self._status = status
        self.shutdown_called = 0
        self.reload_called = 0
        self._reload_raises = reload_raises

    def status(self) -> dict:
        return self._status

    def reload(self, *, config: bool = True) -> dict:
        if self._reload_raises:
            raise RuntimeError("unknown op: 'reload'")
        self.reload_called += 1
        return {"stopped": ["/x"], "reloaded_config": config}

    def shutdown_daemon(self) -> bool:
        self.shutdown_called += 1
        return True

    def close(self) -> None:
        pass


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve() / "state"
        self.base.mkdir()
        self.projects = Path(self.tmp.name).resolve() / "projects"
        self.projects.mkdir()
        self.clients: dict[str, _FakeClient] = {}

    def _state(self, name: str, project: Path) -> Path:
        d = self.base / name
        d.mkdir()
        (d / "project").write_text(str(project) + "\n", encoding="utf-8")
        return d

    def _project(self, name: str) -> Path:
        p = self.projects / name
        p.mkdir(parents=True)
        return p

    def manager(self, *, live: dict, **kw) -> LspEngineManager:
        """``live`` maps project path -> status dict (or None for dead)."""
        m = LspEngineManager(state_base=self.base, **kw)

        def fake_client(root, session, state_dir=None):
            status = live.get(str(root))
            if status is None:
                return None
            c = self.clients.setdefault(str(root), _FakeClient(status))
            return c

        m._client = fake_client            # type: ignore[assignment]
        m._lock_pid = lambda root, state_dir=None: None  # type: ignore[assignment]
        return m


class ListingTests(_Fixture):
    def test_a_running_daemon_is_reported_with_its_engines(self) -> None:
        p = self._project("alpha")
        self._state("aaa", p)
        m = self.manager(live={str(p): {
            "pid": 42, "sessions": ["s1"],
            "engines": [{"root": str(p), "servers": ["tsserver"]}],
            "active_servers": ["tsserver"]}})
        row = m.list()["daemons"][0]
        self.assertTrue(row["running"])
        self.assertEqual(row["pid"], 42)
        self.assertEqual(row["sessions"], ["s1"])
        self.assertEqual(len(row["engines"]), 1)

    def test_a_dead_state_dir_is_reported_not_hidden(self) -> None:
        # Seeing it is how anyone knows there is something to clean.
        p = self._project("beta")
        self._state("bbb", p)
        rows = self.manager(live={}).list()["daemons"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["running"])

    def test_an_orphan_is_flagged_by_its_missing_tree(self) -> None:
        gone = self.projects / "never-existed"
        self._state("ccc", gone)
        row = self.manager(live={str(gone): {"pid": 7}}).list()["daemons"][0]
        self.assertTrue(row["running"])
        self.assertFalse(row["project_exists"])

    def test_listing_never_spawns(self) -> None:
        # A supervisor that started what it was asked to inspect would
        # report a fleet into existence.
        p = self._project("delta")
        self._state("ddd", p)
        spawned = []
        m = self.manager(live={})
        import claude_hooks.lsp_engine.client as C
        orig = C.connect_or_spawn
        C.connect_or_spawn = lambda *a, **k: spawned.append(1)
        self.addCleanup(lambda: setattr(C, "connect_or_spawn", orig))
        m.list()
        self.assertEqual(spawned, [])

    def test_a_state_dir_with_no_project_hint_is_skipped(self) -> None:
        (self.base / "eee").mkdir()
        self.assertEqual(self.manager(live={}).list()["daemons"], [])

    def test_a_disabled_manager_says_so_rather_than_returning_nothing(self) -> None:
        # An empty list reads as "nothing is running", which is the
        # opposite of what someone debugging needs to hear.
        m = LspEngineManager(state_base=self.base, enabled=False)
        res = m.list()
        self.assertFalse(res["available"])
        self.assertNotIn("daemons", res)


class SocketIdentityTests(_Fixture):
    """A state dir is probed at its OWN socket, not a recomputed one.

    Those used to be the same thing. Since the daemon moved to the
    repository boundary they are not, and recomputing gets it wrong in
    both directions — measured during the migration on this host, where
    the manager reported 19 live daemons for 4 processes.
    """

    def test_stale_dirs_under_one_repo_are_not_counted_as_daemons(self) -> None:
        # Pre-boundary state dirs for packages inside a repository all
        # recompute to that repository's socket, so one live daemon gets
        # reported once per stale directory.
        repo = self._project("repo")
        (repo / ".git").mkdir()
        pkg = repo / "packages" / "a"
        pkg.mkdir(parents=True)
        self._state("boundary", repo)
        self._state("stale", pkg)

        probed: list[Path] = []
        m = LspEngineManager(state_base=self.base)

        def fake_client(root, session, state_dir=None):
            probed.append(state_dir)
            if state_dir is not None and state_dir.name == "boundary":
                return _FakeClient({"pid": 1, "project": str(repo),
                                    "sessions": []})
            return None

        m._client = fake_client         # type: ignore[assignment]
        m._lock_pid = lambda root, state_dir=None: None  # type: ignore[assignment]
        rows = m.list()["daemons"]
        # Each state dir was asked about itself, not about the boundary.
        self.assertEqual(sorted(d.name for d in probed if d),
                         ["boundary", "stale"])
        self.assertEqual(len([r for r in rows if r["running"]]), 1)

    def test_a_daemon_serving_more_than_its_hint_says_so(self) -> None:
        # The daemon is the authority on what it owns; the hint file is
        # a breadcrumb that can predate the move to boundaries.
        repo = self._project("repo")
        pkg = repo / "packages" / "a"
        pkg.mkdir(parents=True)
        d = self._state("aaa", pkg)
        m = LspEngineManager(state_base=self.base)
        m._client = lambda root, session, state_dir=None: _FakeClient(
            {"pid": 1, "project": str(repo), "sessions": []})
        m._lock_pid = lambda root, state_dir=None: None  # type: ignore[assignment]
        row = m.list()["daemons"][0]
        self.assertTrue(row["superseded"])
        self.assertEqual(row["serves"], str(repo))
        self.assertEqual(row["state_dir"], str(d))


class StatelessDaemonTests(_Fixture):
    """A daemon whose state dir is gone is invisible to the filesystem.

    Removed by ``cleanup``, by ``restart``, or by this reaper. On POSIX
    the socket inode goes with it, so nothing can connect either — the
    daemon keeps serving the connections it already has and can never be
    reached again. Two existed on this host when the manager was
    written, which is why discovery does not stop at the filesystem.
    """

    def _fake_proc(self, procs: dict[int, list[str]]):
        """A /proc-shaped tree the manager can walk."""
        import claude_hooks.lsp_engine_manager as M
        root = Path(self.tmp.name) / "proc"
        root.mkdir()
        for pid, argv in procs.items():
            d = root / str(pid)
            d.mkdir()
            (d / "cmdline").write_bytes(b"\0".join(
                a.encode() for a in argv) + b"\0")
        (root / "notapid").mkdir()
        orig = M.Path

        # Only the literal Path("/proc") lookup is redirected.
        def fake_path(arg="."):
            return root if str(arg) == "/proc" else orig(arg)

        M.Path = fake_path  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(M, "Path", orig))
        return root

    def test_a_daemon_with_no_hint_anywhere_is_reported(self) -> None:
        p = self._project("lost")
        self._fake_proc({4242: [
            "python", "-m", "claude_hooks.lsp_engine", "daemon",
            "--project", str(p)]})
        rows = LspEngineManager(state_base=self.base).stateless_daemons()
        self.assertEqual([r["pid"] for r in rows], [4242])
        self.assertTrue(rows[0]["stateless"])

    def test_a_discoverable_daemon_is_not_double_reported(self) -> None:
        p = self._project("known")
        self._state("aaa", p)
        self._fake_proc({4242: [
            "python", "-m", "claude_hooks.lsp_engine", "daemon",
            "--project", str(p)]})
        self.assertEqual(
            LspEngineManager(state_base=self.base).stateless_daemons(), [])

    def test_the_hint_is_compared_not_a_recomputed_state_dir(self) -> None:
        # Recomputing normalises a pre-boundary daemon's narrow root up
        # to the repository, which has its own live state dir — so
        # every such daemon looks discoverable and none is reported.
        # The count came back zero against two processes in ``ps``.
        repo = self._project("repo")
        (repo / ".git").mkdir()
        pkg = repo / "packages" / "a"
        pkg.mkdir(parents=True)
        self._state("boundary", repo)          # the repo IS discoverable
        self._fake_proc({4242: [
            "python", "-m", "claude_hooks.lsp_engine", "daemon",
            "--project", str(pkg)]})           # this package is not
        rows = LspEngineManager(state_base=self.base).stateless_daemons()
        self.assertEqual([r["project"] for r in rows], [str(pkg)])

    def test_unrelated_processes_are_ignored(self) -> None:
        self._fake_proc({
            1: ["/sbin/init"],
            2: ["python", "-m", "claude_hooks.lsp_engine", "status",
                "--project", "/x"],
            3: ["python", "-m", "claude_hooks.daemon"],
        })
        self.assertEqual(
            LspEngineManager(state_base=self.base).stateless_daemons(), [])

    def test_a_malformed_argv_does_not_raise(self) -> None:
        self._fake_proc({7: [
            "python", "-m", "claude_hooks.lsp_engine", "daemon",
            "--project"]})          # flag with no value
        self.assertEqual(
            LspEngineManager(state_base=self.base).stateless_daemons(), [])

    def test_they_are_never_auto_reaped(self) -> None:
        # An unlinked socket does not mean nobody is attached: existing
        # connections survive it. Stopping one needs a signal, and a
        # signal to a daemon a live session is still talking to is the
        # user's call, not a reaper's.
        p = self._project("lost")
        self._fake_proc({4242: [
            "python", "-m", "claude_hooks.lsp_engine", "daemon",
            "--project", str(p)]})
        m = self.manager(live={})
        res = m.reap()
        self.assertEqual(res["stopped_orphaned"], [])
        self.assertEqual(res["stopped_idle"], [])


    def test_a_stale_dir_is_not_called_wedged_on_a_neighbours_pid(self) -> None:
        """``wedged`` blocks state cleanup, so a wrong one is permanent.

        ``daemon_pid()`` recomputes the lock path, which normalises a
        stale narrow root up to its repository and returns the boundary
        daemon\'s live pid. Every stale directory under a live repo was
        reported wedged with a pid that is not its own, and could then
        never be cleared. Seen in ``lsp list`` after the deploy.
        """
        repo = self._project("repo")
        (repo / ".git").mkdir()
        pkg = repo / "packages" / "a"
        pkg.mkdir(parents=True)
        boundary = self._state("boundary", repo)
        (boundary / "daemon.lock").write_text("4242\n", encoding="ascii")
        self._state("stale", pkg)          # no lock file of its own

        m = LspEngineManager(state_base=self.base)
        m._client = lambda root, session, state_dir=None: None
        rows = {r["project"]: r for r in m.list()["daemons"]}
        self.assertNotIn("wedged", rows[str(pkg)])
        self.assertIsNone(rows[str(pkg)]["pid"])


class PidReuseTests(_Fixture):
    """A month-old lock naming a live PID is not a wedged daemon.

    PIDs wrap. A lock written 2026-08-20 named pid 3804291, and on
    2026-09-16 a completely unrelated process held that number, so the
    directory was reported ``wedged`` on the strength of a stranger.
    ``wedged`` is what stops the reaper clearing state, so the entry
    could never be cleaned while some process happened to occupy the
    pid. Found in ``lsp list`` output after deploying, not in review.

    The daemon writes ``<pid>\n<unix start time>``, and the second line
    is what makes the first trustworthy.
    """

    def _locked(self, name: str, project: Path, pid: int,
                started: float) -> Path:
        d = self._state(name, project)
        (d / "daemon.lock").write_text(f"{pid}\n{int(started)}\n",
                                       encoding="ascii")
        return d

    def _manager(self) -> LspEngineManager:
        m = LspEngineManager(state_base=self.base)
        m._client = lambda root, session, state_dir=None: None
        return m

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc start times")
    def test_a_reused_pid_is_not_reported_wedged(self) -> None:
        import os
        import time
        p = self._project("stale")
        # This very process: alive, and started long after the lock.
        self._locked("aaa", p, os.getpid(), time.time() - 30 * 86400)
        row = self._manager().list()["daemons"][0]
        self.assertNotIn("wedged", row)
        self.assertIsNone(row["pid"])

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc start times")
    def test_a_genuinely_wedged_daemon_is_still_reported(self) -> None:
        # The check must not swallow the case it was added beside: a
        # process that really is the one the lock names.
        import os
        import time
        p = self._project("wedged")
        self._locked("aaa", p, os.getpid(), time.time() + 60)
        row = self._manager().list()["daemons"][0]
        self.assertTrue(row["wedged"])
        self.assertEqual(row["pid"], os.getpid())

    def test_a_lock_without_a_start_time_is_taken_at_face_value(self) -> None:
        # Locks written by an older daemon have only the pid line.
        import os
        p = self._project("old-format")
        d = self._state("aaa", p)
        (d / "daemon.lock").write_text(f"{os.getpid()}\n", encoding="ascii")
        row = self._manager().list()["daemons"][0]
        self.assertTrue(row["wedged"])

    def test_a_dead_pid_is_not_wedged(self) -> None:
        import time
        p = self._project("dead")
        # PID 1 is init; a pid that cannot be ours and is not running as
        # a daemon. Use an implausible one instead.
        self._locked("aaa", p, 2 ** 22 - 1, time.time())
        row = self._manager().list()["daemons"][0]
        self.assertNotIn("wedged", row)

    def test_an_unreadable_lock_does_not_raise(self) -> None:
        p = self._project("garbage")
        d = self._state("aaa", p)
        (d / "daemon.lock").write_text("not-a-pid\n", encoding="ascii")
        row = self._manager().list()["daemons"][0]
        self.assertIsNone(row["pid"])


class ReapTests(_Fixture):
    def test_an_orphan_is_stopped(self) -> None:
        gone = self.projects / "vanished"
        self._state("aaa", gone)
        m = self.manager(live={str(gone): {"pid": 1, "sessions": []}})
        res = m.reap()
        self.assertEqual(res["stopped_orphaned"], [str(gone)])
        self.assertEqual(self.clients[str(gone)].shutdown_called, 1)

    def test_an_orphan_with_a_session_is_still_stopped(self) -> None:
        # Its tree is gone; there is nothing left for that session to
        # be working on, and the servers are answering about files that
        # do not exist.
        gone = self.projects / "vanished"
        self._state("aaa", gone)
        m = self.manager(live={str(gone): {"pid": 1, "sessions": ["s"]}})
        self.assertEqual(m.reap()["stopped_orphaned"], [str(gone)])

    def test_a_daemon_with_a_session_is_never_reaped_for_idleness(self) -> None:
        import time
        p = self._project("busy")
        self._state("aaa", p)
        m = self.manager(live={str(p): {"pid": 1, "sessions": ["s"]}},
                         idle_seconds=1.0)
        res = m.reap(now=time.monotonic() + 100_000.0)
        self.assertEqual(res["stopped_idle"], [])
        self.assertEqual(self.clients[str(p)].shutdown_called, 0)

    def test_an_idle_daemon_is_stopped_after_the_window(self) -> None:
        import time
        p = self._project("quiet")
        self._state("aaa", p)
        m = self.manager(live={str(p): {"pid": 1, "sessions": []}},
                         idle_seconds=100.0)
        t0 = time.monotonic()
        self.assertEqual(m.reap(now=t0)["stopped_idle"], [])
        self.assertEqual(m.reap(now=t0 + 50)["stopped_idle"], [])
        self.assertEqual(m.reap(now=t0 + 200)["stopped_idle"], [str(p)])

    def test_a_session_resets_the_idle_clock(self) -> None:
        import time
        p = self._project("onoff")
        self._state("aaa", p)
        live = {str(p): {"pid": 1, "sessions": []}}
        m = self.manager(live=live, idle_seconds=100.0)
        t0 = time.monotonic()
        m.reap(now=t0)
        live[str(p)]["sessions"] = ["s"]
        m.reap(now=t0 + 90)
        live[str(p)]["sessions"] = []
        self.assertEqual(m.reap(now=t0 + 150)["stopped_idle"], [])

    def test_the_first_pass_grants_a_grace_period(self) -> None:
        # A fresh supervisor has never observed these daemons, so it
        # must not reap a fleet on the strength of a clock it was not
        # running for.
        import time
        p = self._project("preexisting")
        self._state("aaa", p)
        m = self.manager(live={str(p): {"pid": 1, "sessions": []}},
                         idle_seconds=100.0)
        self.assertEqual(m.reap(now=time.monotonic())["stopped_idle"], [])

    def test_dead_state_for_a_missing_project_is_cleaned(self) -> None:
        gone = self.projects / "removed"
        d = self._state("aaa", gone)
        res = self.manager(live={}).reap()
        self.assertEqual(res["cleaned"], [str(gone)])
        self.assertFalse(d.exists())

    def test_dead_state_for_a_live_project_is_kept(self) -> None:
        # Cheap to keep, and its absence costs a re-resolution. The bar
        # is higher than "nothing is listening right now".
        p = self._project("still-here")
        d = self._state("aaa", p)
        self.assertEqual(self.manager(live={}).reap()["cleaned"], [])
        self.assertTrue(d.exists())

    def test_a_wedged_daemon_keeps_its_state(self) -> None:
        # Socket down, process up. Removing its lock invites a second
        # daemon for the same project.
        gone = self.projects / "wedged"
        d = self._state("aaa", gone)
        m = self.manager(live={})
        m._lock_pid = lambda root, state_dir=None: 999999  # type: ignore[assignment]
        import claude_hooks.lsp_engine.daemon as D
        orig = D.pid_is_alive
        D.pid_is_alive = lambda pid: True
        self.addCleanup(lambda: setattr(D, "pid_is_alive", orig))
        res = m.reap()
        self.assertEqual(res["cleaned"], [])
        self.assertTrue(d.exists())

    def test_reaping_is_disabled_by_a_non_positive_window(self) -> None:
        import time
        p = self._project("forever")
        self._state("aaa", p)
        m = self.manager(live={str(p): {"pid": 1, "sessions": []}},
                         idle_seconds=0.0)
        m.reap(now=time.monotonic())
        self.assertEqual(
            m.reap(now=time.monotonic() + 1e9)["stopped_idle"], [])

    def test_orphan_reaping_can_be_turned_off(self) -> None:
        gone = self.projects / "vanished"
        self._state("aaa", gone)
        m = self.manager(live={str(gone): {"pid": 1, "sessions": []}},
                         reap_orphans=False)
        self.assertEqual(m.reap()["stopped_orphaned"], [])


class ReloadTests(_Fixture):
    def test_fleet_wide_reload_reaches_every_daemon(self) -> None:
        # The case that did not exist: an upgrade, or an edit to a
        # shared cclsp.json, changes what every daemon should run.
        a, b = self._project("a"), self._project("b")
        self._state("aaa", a)
        self._state("bbb", b)
        m = self.manager(live={str(a): {"pid": 1}, str(b): {"pid": 2}})
        res = m.reload()
        self.assertEqual(len(res["results"]), 2)
        self.assertTrue(all(r["reloaded"] for r in res["results"]))

    def test_one_project_reloads_only_itself(self) -> None:
        a, b = self._project("a"), self._project("b")
        self._state("aaa", a)
        self._state("bbb", b)
        m = self.manager(live={str(a): {"pid": 1}, str(b): {"pid": 2}})
        res = m.reload(a)
        self.assertEqual([r["project"] for r in res["results"]], [str(a)])
        self.assertNotIn(str(b), self.clients)

    def test_a_daemon_that_is_not_running_is_reported_not_skipped(self) -> None:
        a = self._project("a")
        self._state("aaa", a)
        res = self.manager(live={}).reload()
        self.assertFalse(res["results"][0]["reloaded"])
        self.assertEqual(res["results"][0]["reason"], "not running")

    def test_a_daemon_predating_reload_names_itself(self) -> None:
        # It is a long-lived process holding the code it imported, so
        # the remedy differs: that one has to be stopped.
        a = self._project("a")
        self._state("aaa", a)
        m = self.manager(live={str(a): {"pid": 1}})
        self.clients[str(a)] = _FakeClient({"pid": 1}, reload_raises=True)
        res = m.reload()
        self.assertFalse(res["results"][0]["reloaded"])
        self.assertIn("unknown op", res["results"][0]["reason"])

    def test_one_failure_does_not_stop_the_fleet(self) -> None:
        a, b = self._project("a"), self._project("b")
        self._state("aaa", a)
        self._state("bbb", b)
        m = self.manager(live={str(a): {"pid": 1}, str(b): {"pid": 2}})
        self.clients[str(a)] = _FakeClient({"pid": 1}, reload_raises=True)
        res = m.reload()
        ok = [r for r in res["results"] if r["reloaded"]]
        self.assertEqual(len(ok), 1)


class ShutdownTests(_Fixture):
    def test_shutting_down_the_supervisor_leaves_the_daemons_running(self) -> None:
        # A claude-hooks restart during a deploy must not cost every
        # open session its warm language servers.
        p = self._project("keepme")
        self._state("aaa", p)
        m = self.manager(live={str(p): {"pid": 1, "sessions": ["s"]}})
        m.list()
        m.shutdown()
        client = self.clients.get(str(p))
        self.assertEqual(0 if client is None else client.shutdown_called, 0)

    def test_stop_asks_one_daemon_to_go(self) -> None:
        p = self._project("stopme")
        self._state("aaa", p)
        m = self.manager(live={str(p): {"pid": 1}})
        res = m.stop(p)
        self.assertTrue(res["results"][0]["stopped"])
        self.assertEqual(self.clients[str(p)].shutdown_called, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
