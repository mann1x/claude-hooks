"""Daemon-side mailbox lifecycle.

``archive.sweep()`` was implemented and tested from the start and
nothing called it, which is a different failure from a broken sweep and
looks identical from the outside: mail expires and stays, the archive
never fills, and the registry accumulates sessions that ended weeks
ago. These cover the scheduling, not the policy.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.mailbox import maintenance  # noqa: E402
from claude_hooks.mailbox.store import MailboxStore, utcnow  # noqa: E402

ENABLED = {"hooks": {"mailbox": {"enabled": True}}}
DISABLED = {"hooks": {"mailbox": {"enabled": False}}}


class _Conn:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.RLock()

    def __call__(self):
        return self.conn

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class _Provider:
    """Minimal pgvector-shaped stand-in: a name and a borrowed handle."""

    name = "pgvector"

    def __init__(self, conn: _Conn):
        self._conn_holder = conn
        self._lock = conn.lock
        self._conn = conn.conn

    def _ensure_ready(self):
        return None


class EnabledTests(unittest.TestCase):

    def test_disabled_mailbox_is_not_swept(self):
        self.assertIsNone(maintenance.run_sweep(DISABLED))

    def test_absent_config_is_not_swept(self):
        self.assertIsNone(maintenance.run_sweep({}))
        self.assertIsNone(maintenance.run_sweep(None))

    def test_maintenance_can_be_turned_off_independently(self):
        cfg = {"hooks": {"mailbox": {"enabled": True,
                                     "maintenance": False}}}
        self.assertIsNone(maintenance.run_sweep(cfg))

    def test_enabled_by_default_when_the_mailbox_is_on(self):
        self.assertTrue(maintenance._enabled(ENABLED))


class SettingsTests(unittest.TestCase):

    def test_defaults(self):
        s = maintenance._settings(ENABLED)
        self.assertEqual(s["interval"],
                         maintenance.DEFAULT_INTERVAL_SECONDS)
        self.assertEqual(s["limit"], maintenance.DEFAULT_LIMIT)
        self.assertIsNone(s["registry_days"])
        self.assertIsNone(s["cap_bytes"])

    def test_overrides_are_read(self):
        s = maintenance._settings({"hooks": {"mailbox": {
            "enabled": True,
            "maintenance_interval_seconds": 120,
            "maintenance_limit": 25,
            "registry_days": 7,
            "archive_cap_bytes": 1024,
        }}})
        self.assertEqual(s["interval"], 120.0)
        self.assertEqual(s["limit"], 25)
        self.assertEqual(s["registry_days"], 7)
        self.assertEqual(s["cap_bytes"], 1024)

    def test_nonsense_falls_back_rather_than_raising(self):
        # A bad number in config must not stop maintenance entirely;
        # the daemon's stance on optional subsystems is fail-open.
        s = maintenance._settings({"hooks": {"mailbox": {
            "enabled": True,
            "maintenance_interval_seconds": "soon",
            "maintenance_limit": None,
            "registry_days": "many",
        }}})
        self.assertEqual(s["interval"],
                         maintenance.DEFAULT_INTERVAL_SECONDS)
        self.assertEqual(s["limit"], maintenance.DEFAULT_LIMIT)
        self.assertIsNone(s["registry_days"])


class SweepTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = _Conn(self.tmp / "m.db")
        self.addCleanup(self.db.close)
        self.store = MailboxStore(self.db, self.db.lock, dialect="sqlite")
        self.store.ensure_schema()
        self.provider = _Provider(self.db)

        # Route store_for_provider at our harness connection rather than
        # letting it build one from a real provider.
        patcher = mock.patch(
            "claude_hooks.mailbox.integration.store_for_provider",
            lambda _p: self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.archive_dir = self.tmp / "archive"
        ap = mock.patch("claude_hooks.mailbox.archive.archive_dir",
                        lambda: self.archive_dir)
        ap.start()
        self.addCleanup(ap.stop)

    def _expire_everything(self):
        past = (utcnow() - timedelta(days=1)).isoformat()
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE session_messages SET expires_at = ?", (past,))
            self.db.conn.commit()

    def test_expired_mail_is_archived_then_deleted(self):
        self.store.register("s1", "alice", host="h1")
        self.store.send("alice", "subject", "body", from_alias="bob")
        self._expire_everything()

        report = maintenance.run_sweep(ENABLED, providers=[self.provider])
        self.assertIsNotNone(report)
        self.assertEqual(report["expired"], 1)
        self.assertEqual(report["deleted"], 1)
        self.assertTrue(report["archived"], "nothing was archived")
        self.assertEqual(self.store.count_messages()
                         if hasattr(self.store, "count_messages")
                         else len(self.store.expired(limit=10)), 0)

    def test_unexpired_mail_is_left_alone(self):
        self.store.register("s1", "alice", host="h1")
        self.store.send("alice", "subject", "body", from_alias="bob")
        report = maintenance.run_sweep(ENABLED, providers=[self.provider])
        self.assertEqual(report["expired"], 0)
        self.assertEqual(report["deleted"], 0)
        rows = self.store.inbox(alias="alice", host="h1")
        self.assertEqual(len(rows), 1)

    def test_stale_registry_entries_are_forgotten(self):
        self.store.register("old", "ghost", host="h1")
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE session_registry SET last_seen = ?",
                ((utcnow() - timedelta(days=40)).isoformat(),))
            self.db.conn.commit()
        report = maintenance.run_sweep(ENABLED, providers=[self.provider])
        self.assertEqual(report["sessions_forgotten"], 1)
        self.assertEqual(self.store.sessions(), [])

    def test_a_live_session_survives_the_sweep(self):
        self.store.register("live", "worker", host="h1")
        report = maintenance.run_sweep(ENABLED, providers=[self.provider])
        self.assertEqual(report["sessions_forgotten"], 0)
        self.assertEqual(len(self.store.sessions()), 1)

    def test_no_sql_provider_returns_none_not_an_empty_report(self):
        # "Nothing to do" and "there was no mailbox to look at" must not
        # render the same, or a host where this silently never runs
        # looks exactly like a host where it runs and finds nothing.
        with mock.patch(
                "claude_hooks.mailbox.integration.store_for_provider",
                lambda _p: None):
            self.assertIsNone(
                maintenance.run_sweep(ENABLED, providers=[self.provider]))

    def test_a_failing_sweep_is_swallowed(self):
        with mock.patch("claude_hooks.mailbox.archive.sweep",
                        side_effect=RuntimeError("db gone")):
            self.assertIsNone(
                maintenance.run_sweep(ENABLED, providers=[self.provider]))

    def test_limit_is_passed_through(self):
        seen = {}

        def fake_sweep(_store, **kw):
            seen.update(kw)
            return {"expired": 0, "archived": [], "deleted": 0,
                    "dropped": [], "sessions_forgotten": 0}

        cfg = {"hooks": {"mailbox": {"enabled": True,
                                     "maintenance_limit": 7,
                                     "registry_days": 3,
                                     "archive_cap_bytes": 99}}}
        with mock.patch("claude_hooks.mailbox.archive.sweep", fake_sweep):
            maintenance.run_sweep(cfg, providers=[self.provider])
        self.assertEqual(seen["limit"], 7)
        self.assertEqual(seen["registry_days"], 3)
        self.assertEqual(seen["cap_bytes"], 99)

    def test_defaults_are_not_forced_onto_sweep(self):
        # archive.sweep owns the retention defaults; passing None would
        # override them with nothing.
        seen = {}

        def fake_sweep(_store, **kw):
            seen.update(kw)
            return {"expired": 0, "archived": [], "deleted": 0,
                    "dropped": [], "sessions_forgotten": 0}

        with mock.patch("claude_hooks.mailbox.archive.sweep", fake_sweep):
            maintenance.run_sweep(ENABLED, providers=[self.provider])
        self.assertNotIn("registry_days", seen)
        self.assertNotIn("cap_bytes", seen)
        self.assertEqual(seen["limit"], maintenance.DEFAULT_LIMIT)


class ThreadTests(unittest.TestCase):

    def test_thread_is_a_daemon_thread(self):
        t = maintenance.MailboxMaintenanceThread(
            config_loader=lambda: ENABLED, stop_event=threading.Event())
        self.assertTrue(t.daemon,
                        "must not keep the daemon process alive at exit")

    def test_initial_delay_is_respected_on_stop(self):
        stop = threading.Event()
        stop.set()
        calls = []
        with mock.patch.object(maintenance, "run_sweep",
                               lambda *_a, **_k: calls.append(1)):
            t = maintenance.MailboxMaintenanceThread(
                config_loader=lambda: ENABLED, stop_event=stop,
                initial_delay_seconds=0.01)
            t.run()
        self.assertEqual(calls, [],
                         "a stop during the initial delay must not sweep")

    def test_interval_floor_prevents_a_hot_loop(self):
        # A configured 0 would otherwise spin on the same connection the
        # hooks are waiting for — worse than stale mail.
        self.assertEqual(maintenance.sleep_seconds(0),
                         maintenance.MIN_INTERVAL_SECONDS)
        self.assertEqual(maintenance.sleep_seconds(-5),
                         maintenance.MIN_INTERVAL_SECONDS)

    def test_a_longer_interval_is_honoured(self):
        self.assertEqual(maintenance.sleep_seconds(7200.0), 7200.0)

    def test_an_unusable_interval_falls_back_to_the_default(self):
        self.assertEqual(maintenance.sleep_seconds("soon"),
                         maintenance.DEFAULT_INTERVAL_SECONDS)


class DaemonWiringTests(unittest.TestCase):
    """The reason this file exists is that nothing called the sweep."""

    def test_daemon_starts_the_thread(self):
        import claude_hooks.daemon as daemon_mod
        src = Path(daemon_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("MailboxMaintenanceThread", src)
        self.assertIn("mailbox_thread.start()", src)

    def test_daemon_joins_the_thread_on_shutdown(self):
        import claude_hooks.daemon as daemon_mod
        src = Path(daemon_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("mailbox_thread.join", src)


class TouchTests(unittest.TestCase):
    """``last_seen`` only moved at SessionStart, so a session open for
    longer than the registry TTL was swept while in use."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = _Conn(Path(self._tmp.name) / "m.db")
        self.addCleanup(self.db.close)
        self.store = MailboxStore(self.db, self.db.lock, dialect="sqlite")
        self.store.ensure_schema()

    def test_announce_touches_the_session(self):
        from claude_hooks.mailbox import hook as hookmod
        from claude_hooks.mailbox.tools import MailboxTools

        self.store.register("mine", "me", host="solidpc")
        tools = MailboxTools(self.store, alias="me", session_id="mine",
                             host="solidpc")
        touched = []
        with mock.patch.object(type(self.store), "touch",
                               lambda _s, sid: touched.append(sid)):
            hookmod._tools = lambda config, providers, event=None: tools
            try:
                hookmod.announce_block(
                    event={"session_id": "mine"}, config=ENABLED,
                    providers=[object()])
            finally:
                import importlib
                importlib.reload(hookmod)
        self.assertEqual(touched, ["mine"])

    def test_a_failing_touch_does_not_break_the_announcement(self):
        from claude_hooks.mailbox import hook as hookmod
        from claude_hooks.mailbox.tools import MailboxTools

        self.store.register("mine", "me", host="solidpc")
        self.store.send("me", "hello", "body", from_alias="them")
        tools = MailboxTools(self.store, alias="me", session_id="mine",
                             host="solidpc")
        with mock.patch.object(type(self.store), "touch",
                               side_effect=RuntimeError("db gone")):
            hookmod._tools = lambda config, providers, event=None: tools
            try:
                out = hookmod.announce_block(
                    event={"session_id": "mine"}, config=ENABLED,
                    providers=[object()])
            finally:
                import importlib
                importlib.reload(hookmod)
        self.assertIn("hello", out)


if __name__ == "__main__":
    unittest.main()
