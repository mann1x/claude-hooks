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

        def fake_client(root, session):
            status = live.get(str(root))
            if status is None:
                return None
            c = self.clients.setdefault(str(root), _FakeClient(status))
            return c

        m._client = fake_client            # type: ignore[assignment]
        m._lock_pid = lambda root: None    # type: ignore[assignment]
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
        m._lock_pid = lambda root: 999999      # type: ignore[assignment]
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
