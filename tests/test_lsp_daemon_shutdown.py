"""A daemon that cannot be stopped is the bug this file guards.

Three defects met on solidpc on 2026-09-17 to leave one daemon alive,
serving code from before two deploys, holding the lock that would let a
replacement spawn, and answering nothing — and to have the deploy that
was supposed to have stopped it report success:

1. ``socketserver``'s request threads are non-daemon and joined on
   close, so one client that connected and went away without closing
   pinned the process open through ``server_close`` *and* interpreter
   shutdown.
2. ``Daemon.stop`` guarded on "a stop was requested" rather than "the
   teardown ran", so the SIGTERM path — where the signal handler sets
   that very flag — tore down nothing.
3. The supervisor took the shutdown *ack* for an exit. The ack is sent
   before teardown begins, by design, so a daemon that hangs during
   teardown reports itself stopped.

Each is covered here, plus the process-start-time reading that hid the
wedged daemon from ``lsp list`` while all this was going on.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine import ipc as ipc_mod  # noqa: E402
from claude_hooks.lsp_engine.daemon import (  # noqa: E402
    pid_is_alive,
    process_start_time,
)
from claude_hooks.lsp_engine_manager import LspEngineManager  # noqa: E402

POSIX_ONLY = unittest.skipUnless(
    os.name == "posix", "unix-socket server; Windows uses named pipes")
PROC_ONLY = unittest.skipUnless(
    Path("/proc").is_dir(), "reads /proc")


class ProcessStartTimeTests(unittest.TestCase):
    """The clock the PID-reuse guard and the deploy check both read."""

    @PROC_ONLY
    def test_it_matches_what_ps_reports(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        got = process_start_time(proc.pid)
        self.assertIsNotNone(got)
        # ``ps`` is the independent authority; epoch seconds, so this
        # compares the same quantity rather than two format guesses.
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(proc.pid)],
            capture_output=True, text=True)
        if out.returncode != 0 or not out.stdout.strip():
            self.skipTest("ps unavailable")
        import datetime
        ref = datetime.datetime.strptime(
            out.stdout.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
        self.assertLess(abs(got - ref), 2.0,
                        f"start time {got} disagrees with ps {ref}")

    @PROC_ONLY
    def test_it_is_not_the_proc_directory_mtime(self) -> None:
        """The mistake this function exists to stop being made again.

        ``/proc/<pid>`` has an mtime, it is a plausible-looking number,
        and it is not the start time — the kernel updates it afterwards.
        The daemon that prompted all this started at 19:40:20 and its
        directory mtime read 02:03:30 the next morning, six hours out,
        which was enough for the PID-reuse guard to decide the daemon's
        own lock belonged to a stranger and hide it from ``lsp list``.

        Asserting a *difference* would be flaky — on a fresh process the
        two can still agree. So this pins the source instead: the value
        must track the real start even when the directory is touched.
        """
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        first = process_start_time(proc.pid)
        # Anything that makes the kernel refresh the directory. The
        # start time must not move with it.
        for _ in range(3):
            os.listdir(f"/proc/{proc.pid}")
            Path(f"/proc/{proc.pid}/stat").read_text()
            time.sleep(0.05)
        self.assertEqual(first, process_start_time(proc.pid))

    def test_a_dead_pid_has_no_start_time(self) -> None:
        self.assertIsNone(process_start_time(0))
        self.assertIsNone(process_start_time(2 ** 30))


@POSIX_ONLY
class HungClientTests(unittest.TestCase):
    """One silent client must not be able to hold the daemon open."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sock_path = Path(self.tmp.name) / "d.sock"
        self.server = ipc_mod.IpcServer(
            self.sock_path, lambda req: {"id": req.get("id"), "ok": True})
        self.server.start_in_background()

    def test_request_threads_are_daemon_and_never_joined(self) -> None:
        # Both defaults are wrong for a stoppable daemon, and both have
        # to be overridden: daemon_threads alone still leaves
        # server_close joining, and block_on_close alone still leaves
        # interpreter shutdown joining.
        cls = ipc_mod._DaemonThreadingUnixStreamServer
        self.assertTrue(cls.daemon_threads)
        self.assertFalse(cls.block_on_close)

    def test_a_silent_client_does_not_block_shutdown(self) -> None:
        """The exact shape of the live incident.

        A client connects and sends nothing. Its request thread parks in
        ``readinto``, which has no timeout, so before the fix
        ``server_close()`` joined it forever and the process could never
        exit — listener already closed, lock still held, nothing able to
        respawn.
        """
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(str(self.sock_path))
        # Let the server accept it and enter the blocking read.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.server._impl._live_conns:
                break
            time.sleep(0.02)
        self.assertTrue(self.server._impl._live_conns,
                        "server never began serving the connection")

        done = threading.Event()
        err: list[BaseException] = []

        def _shutdown():
            try:
                self.server.shutdown()
            except BaseException as e:      # pragma: no cover — defensive
                err.append(e)
            finally:
                done.set()

        t = threading.Thread(target=_shutdown, daemon=True)
        t.start()
        self.assertTrue(done.wait(timeout=15.0),
                        "shutdown hung on a silent client")
        self.assertEqual(err, [])

    def test_an_in_flight_reply_survives_the_shutdown(self) -> None:
        """The ack must outlive the teardown it triggers.

        ``shutdown`` answers first and tears down after, so the reply is
        written while the teardown is already running. Closing live
        connections with no grace truncated it, and every
        ``shutdown_daemon()`` then reported failure for a daemon that
        had in fact stopped — swapping one misleading success for one
        misleading failure.
        """
        import json

        def _slow(req):
            time.sleep(0.2)
            return {"id": req.get("id"), "ok": True}

        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "slow.sock"
        server = ipc_mod.IpcServer(path, _slow)
        server.start_in_background()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(str(path))
        client.sendall(json.dumps({"id": 1, "op": "x"}).encode() + b"\n")
        time.sleep(0.05)                 # handler is now mid-flight
        threading.Thread(target=server.shutdown, daemon=True).start()
        client.settimeout(10.0)
        data = client.makefile("rb").readline()
        self.assertTrue(data, "the reply was cut off by the shutdown")
        self.assertTrue(json.loads(data)["ok"])

    def test_the_served_connection_is_released(self) -> None:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(str(self.sock_path))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self.server._impl._live_conns:
            time.sleep(0.02)
        self.server.shutdown()
        # Closing the sockets is what unblocks the request threads;
        # leaving them registered would leak an fd per dead client.
        self.assertFalse(self.server._impl._live_conns)


class ImportsWithoutUnixSocketsTests(unittest.TestCase):
    """The ipc module must import where unix sockets do not exist.

    Windows has no ``socketserver.ThreadingUnixStreamServer`` — it uses
    named pipes, and the POSIX backend is simply never started there. So
    a reference to that name inside ``start()`` costs nothing, and the
    same reference as a base class at module level breaks the import,
    taking every lsp test on the host with it. That is what happened on
    pandorum, and it would have shipped had the suite not run there.

    Reproducing it on Linux is the point: a platform-only failure that
    can only be seen on the other platform gets found late, by someone
    else, every time.
    """

    def test_it_imports_when_the_unix_server_is_absent(self) -> None:
        import importlib.util
        import socketserver as ss

        # Loaded as a *separate* module object, never through
        # sys.modules. ``importlib.reload`` would rebind the real one,
        # and every class it defines with it — which quietly broke the
        # Windows-dispatch tests that run after this file and compare
        # against those classes by identity.
        saved = getattr(ss, "ThreadingUnixStreamServer", None)
        if saved is not None:
            del ss.ThreadingUnixStreamServer
        try:
            spec = importlib.util.spec_from_file_location(
                "_ipc_without_unix_sockets",
                REPO / "claude_hooks" / "lsp_engine" / "ipc.py")
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)       # must not raise
            self.assertTrue(hasattr(mod, "IpcServer"))
        finally:
            if saved is not None:
                ss.ThreadingUnixStreamServer = saved
        # And the real module is untouched.
        self.assertTrue(hasattr(ipc_mod, "IpcServer"))


class StopIdempotenceTests(unittest.TestCase):
    """``stop()`` must tear down on the signal path, not just the op."""

    def _daemon(self):
        import tempfile
        from claude_hooks.lsp_engine.daemon import Daemon
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "cclsp.json").write_text('{"servers": []}', encoding="utf-8")
        return Daemon(root, [], state_base=root / "state")

    def test_stop_runs_after_stopping_was_already_set(self) -> None:
        """SIGTERM's handler sets ``_stopping``; ``run()`` then calls
        ``stop()``. Guarding on that flag made the whole teardown a
        no-op for every signal-initiated shutdown."""
        d = self._daemon()
        d._stopping.set()               # what the signal handler does
        calls = []
        d._pool.shutdown = lambda: calls.append("pool")  # type: ignore
        d.stop()
        self.assertEqual(calls, ["pool"],
                         "stop() skipped teardown because a stop was pending")

    def test_stop_is_still_idempotent(self) -> None:
        d = self._daemon()
        calls = []
        d._pool.shutdown = lambda: calls.append("pool")  # type: ignore
        d.stop()
        d.stop()
        d.stop()
        self.assertEqual(calls, ["pool"])


class VerifiedStopTests(unittest.TestCase):
    """The supervisor reports exits, not acks."""

    def setUp(self) -> None:
        self.m = LspEngineManager()

    def test_an_ack_without_an_exit_escalates(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(120)"])
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())

        class _FakeClient:
            def shutdown_daemon(self):  # acks, changes nothing
                return {"ok": True}

            def close(self):
                pass

        self.m._lock_pid = lambda root, sd=None: proc.pid   # type: ignore
        self.m._client = lambda *a, **k: _FakeClient()      # type: ignore
        # It is not an lsp_engine daemon, so the identity guard must
        # refuse to signal it — and then must not claim it stopped.
        res = self.m._stop_one_detailed(Path("/nonexistent"), wait_s=0.5)
        self.assertTrue(res["acked"])
        self.assertIsNone(res["signalled"])
        self.assertFalse(res["stopped"],
                         "reported stopped while the process was alive")
        self.assertTrue(pid_is_alive(proc.pid))

    @PROC_ONLY
    def test_a_real_daemon_process_is_escalated_to(self) -> None:
        """With the identity guard satisfied, the ladder runs.

        The stand-in ignores SIGTERM, which is the case the ladder
        exists for: a daemon running pre-fix code that hangs where the
        current source no longer can.
        """
        # The guard matches on argv, and ``-c`` puts the source there,
        # so the marker goes in the source itself.
        script = ("# stand-in for claude_hooks.lsp_engine\n"
                  "import signal, time\n"
                  "signal.signal(signal.SIGTERM, lambda *a: None)\n"
                  "time.sleep(120)\n")
        proc = subprocess.Popen([sys.executable, "-c", script])
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        # argv is not readable the instant Popen returns — the child may
        # still be mid-exec, and /proc then shows the pre-exec image.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.m._is_lsp_daemon(proc.pid):
                break
            time.sleep(0.02)
        self.assertTrue(self.m._is_lsp_daemon(proc.pid))

        self.m._lock_pid = lambda root, sd=None: proc.pid   # type: ignore
        self.m._client = lambda *a, **k: None               # type: ignore
        res = self.m._stop_one_detailed(Path("/nonexistent"), wait_s=1.0)
        self.assertTrue(res["stopped"])
        self.assertEqual(res["signalled"], "SIGKILL",
                         "SIGTERM was ignored; the ladder must go further")

    @PROC_ONLY
    def test_a_process_that_is_not_ours_is_never_signalled(self) -> None:
        # The guard that stands between a stale lock file and an
        # unrelated process on a recycled pid.
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertFalse(self.m._is_lsp_daemon(proc.pid))

    def test_an_unreadable_pid_is_not_ours(self) -> None:
        self.assertFalse(self.m._is_lsp_daemon(2 ** 30))


class CloseAfterDaemonWentAwayTests(unittest.TestCase):
    """Closing a connection to a daemon that has stopped is not an error.

    It became reachable as soon as the daemon started exiting *promptly*
    after ``shutdown``. Until then the process lingered — interpreter
    shutdown was joining the non-daemon request thread — so a ``detach``
    sent afterwards still found someone to answer it. With the request
    threads made daemon threads, the process goes at once and the
    ``detach`` hits a closed socket, which is the correct behaviour on
    both sides. Only ``close()`` had to stop treating it as a failure.
    """

    def test_close_does_not_raise_when_the_daemon_is_gone(self) -> None:
        from claude_hooks.lsp_engine.client import LspEngineClient
        from claude_hooks.lsp_engine.ipc import IpcProtocolError

        client = LspEngineClient.__new__(LspEngineClient)

        class _Ipc:
            closed = False

            def close(self):
                type(self).closed = True

        client._ipc = _Ipc()                                # type: ignore
        client.detach = lambda: (_ for _ in ()).throw(      # type: ignore
            IpcProtocolError("daemon closed connection"))
        client.close()                       # must not raise
        self.assertTrue(_Ipc.closed, "the connection was left open")


class DerivedStateDirTests(unittest.TestCase):
    """A caller that omits the state dir must not lose the proof.

    ``_may_signal`` has two identity proofs: the command line, and the
    start time recorded in the lock file. The lock lives in the state
    dir, so passing None dropped the second entirely — leaving only
    ``/proc/<pid>/cmdline``, which a process already on its way out has
    emptied. A live daemon was then declined for a signal *and* reported
    as one that would not stop, seconds before it exited on its own.
    Harmless when nothing read the result; not harmless now that deploy
    fails the step on it.
    """

    def test_the_state_dir_is_derived_when_not_given(self) -> None:
        import tempfile
        m = LspEngineManager()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            seen = {}

            def _lock_pid(r, sd=None):
                seen["state_dir"] = sd
                return None

            m._lock_pid = _lock_pid                  # type: ignore
            m._client = lambda *a, **k: None         # type: ignore
            m._stop_one_detailed(root, wait_s=0.1)
        self.assertIsNotNone(seen["state_dir"],
                             "state dir was left None, dropping the "
                             "start-time identity proof")

    def test_a_late_exit_is_still_a_stop(self) -> None:
        """The process goes after the ladder gives up, not before."""
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(0.8)"])
        self.addCleanup(proc.wait)
        m = LspEngineManager()
        m._lock_pid = lambda r, sd=None: proc.pid    # type: ignore
        m._client = lambda *a, **k: None             # type: ignore
        m._may_signal = lambda *a, **k: False        # type: ignore
        res = m._stop_one_detailed(Path("/nonexistent"), wait_s=0.1)
        self.assertTrue(res["was_running"])
        self.assertTrue(res["stopped"],
                        "declared a survivor while it was exiting")


class StoppedMeansStoppedTests(unittest.TestCase):
    """The count has to mean what an operator reads it as meaning."""

    def test_a_dead_lock_file_is_not_a_stopped_daemon(self) -> None:
        """Most state dirs on a long-lived host hold a lock naming a pid
        that died weeks ago. Counting those as stops reported "stopped
        139" on a host running one daemon — the same inflated figure as
        the old "stopped 2 of 149", just laundered through a different
        field."""
        m = LspEngineManager()
        m._lock_pid = lambda root, sd=None: 2 ** 30   # long dead
        m._client = lambda *a, **k: None              # nothing listening
        res = m._stop_one_detailed(Path("/nonexistent"), wait_s=0.1)
        self.assertFalse(res["was_running"])
        self.assertFalse(res["stopped"])
        self.assertIsNone(res["signalled"])

    def test_a_dead_lock_file_is_not_a_survivor_either(self) -> None:
        # The same conflation read the other way round would fail a
        # deploy over 139 daemons that were never there.
        m = LspEngineManager()
        m._lock_pid = lambda root, sd=None: 2 ** 30
        m._client = lambda *a, **k: None
        res = m._stop_one_detailed(Path("/nonexistent"), wait_s=0.1)
        survived = res["was_running"] and not res["stopped"]
        self.assertFalse(survived)


class SignalLadderTests(unittest.TestCase):
    """The ladder has to exist on the platform it runs on."""

    def test_posix_climbs_to_sigkill(self) -> None:
        import signal as real
        if not hasattr(real, "SIGKILL"):
            self.skipTest("no SIGKILL on this platform")
        rungs = LspEngineManager._signal_rungs(real)
        self.assertEqual([label for _, label in rungs],
                         ["SIGTERM", "SIGKILL"])

    def test_windows_stops_at_sigterm(self) -> None:
        """Windows has no SIGKILL, and naming it raised out of every
        stop on pandorum. SIGTERM there is already TerminateProcess, so
        one rung is the whole ladder."""
        class _NoSigkill:
            SIGTERM = 15
        rungs = LspEngineManager._signal_rungs(_NoSigkill)
        self.assertEqual([label for _, label in rungs], ["SIGTERM"])


class WedgedReapTests(unittest.TestCase):
    """A wedged daemon is an outage, and the reaper now clears it."""

    def test_reap_stops_a_wedged_daemon(self) -> None:
        m = LspEngineManager()
        # ``reap`` round-trips the project through ``Path``, which on
        # Windows respells the separators, so the expectation has to go
        # through the same normalisation rather than assume POSIX.
        project = "/tmp/gone-wedged"
        expected = str(Path(project))
        entry = {"project": project, "state_dir": "/tmp/sd",
                 "running": False, "wedged": True, "pid": 424242,
                 "sessions": [], "project_exists": False}
        m.list = lambda: {"daemons": [entry]}          # type: ignore
        stopped: list = []
        m._stop_one = lambda root, sd=None: (          # type: ignore
            stopped.append(str(root)) or True)
        m._clean_state = lambda e: True                # type: ignore
        res = m.reap()
        self.assertEqual(stopped, [expected])
        self.assertEqual(res["stopped_wedged"], [expected])

    def test_a_wedged_daemon_that_will_not_die_keeps_its_state(self) -> None:
        # Removing the lock of a live process invites a second daemon
        # for the same project, so cleanup stays gated on a real stop.
        m = LspEngineManager()
        entry = {"project": "/tmp/stubborn", "state_dir": "/tmp/sd2",
                 "running": False, "wedged": True, "pid": 424243,
                 "sessions": [], "project_exists": False}
        m.list = lambda: {"daemons": [entry]}          # type: ignore
        m._stop_one = lambda root, sd=None: False      # type: ignore
        cleaned: list = []
        m._clean_state = lambda e: cleaned.append(e) or True  # type: ignore
        res = m.reap()
        self.assertEqual(res["stopped_wedged"], [])
        self.assertEqual(cleaned, [], "cleared the state of a live daemon")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
