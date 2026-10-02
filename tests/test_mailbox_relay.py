"""Cloud-session mailbox relay: the semaphore protocol, the round trip
through the real mailbox, the watchers, and what it costs while idle.

The store is a real SQLite mailbox (the same harness as test_mailbox),
so every request runs the production SQL and ownership rules.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_hooks.mailbox import relay, watch
from claude_hooks.mailbox import store as store_mod
from claude_hooks.mailbox.relay import (
    ALIAS_OP, MailboxRelayThread, RelayCore, install_instructions,
    read_semaphore, settings, write_pair,
)
from claude_hooks.mailbox.store import MailboxStore
from claude_hooks.mailbox.tools import MailboxTools


class _SqliteConn:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.RLock()

    def __call__(self):
        return self.conn

    def close(self):
        self.conn.close()


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def opts(**over) -> dict:
    o = settings({"hooks": {"mailbox": {"enabled": True, "cloud_relay": {
        "enabled": True, "root": "/unused"}}}})
    o.update(over)
    return o


class RelayHarness(unittest.TestCase):
    HOST = "solidpc"

    def setUp(self):
        p = mock.patch.object(store_mod, "host_name", lambda: self.HOST)
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.db = _SqliteConn(self.tmp / "m.db")
        self.addCleanup(self.db.close)
        self.store = MailboxStore(self.db, self.db.lock, dialect="sqlite")
        self.store.ensure_schema()
        self.root = self.tmp / "mailbox"
        (self.root / "sessions").mkdir(parents=True)
        # Real time: semaphore ages come from real file mtimes.
        self.clock = Clock(time.time())
        self.factory_calls = 0
        self.core = self.make_core()

    def make_core(self, **over) -> RelayCore:
        def factory():
            self.factory_calls += 1
            return self.store
        return RelayCore(self.root, opts(**over), store_factory=factory,
                         clock=self.clock)

    # -- what a cloud session does, by the book
    def req_dir(self, alias="osync") -> Path:
        d = self.root / "sessions" / alias / "requests"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_request(self, rid, tool, args=None, *, alias="osync",
                      status="ready", with_bytes=False, op=None):
        d = self.req_dir(alias)
        op = tool if op is None else op
        (d / f"{rid}.sem").write_text(json.dumps(
            {"op": op, "status": "writing"}), encoding="utf-8")
        body = json.dumps({"tool": tool, "args": args or {}})
        (d / f"{rid}.json").write_text(body, encoding="utf-8")
        sem = {"op": op, "status": status}
        if with_bytes:
            sem["bytes"] = len(body.encode())
        (d / f"{rid}.sem").write_text(json.dumps(sem), encoding="utf-8")

    def reply(self, rid, alias="osync") -> str:
        d = self.root / "sessions" / alias / "replies"
        sem = read_semaphore(d / f"{rid}.sem")
        self.assertIsNotNone(sem, f"no reply semaphore for {rid}")
        self.assertEqual(sem.status, "ready")
        text = (d / f"{rid}.md").read_text(encoding="utf-8")
        self.assertEqual(sem.bytes, len(text.encode()))
        return text

    def take_alias(self, alias="osync"):
        self.write_request("alias", ALIAS_OP, {"alias": alias}, alias=alias)
        self.core.process_alias(alias)
        return self.reply("alias", alias)

    def local(self, alias="claude-hooks") -> MailboxTools:
        self.store.register(f"{alias}-sid", alias, host=self.HOST)
        return MailboxTools(self.store, alias=alias,
                            session_id=f"{alias}-sid", host=self.HOST)


class SemaphoreProtocolTests(RelayHarness):

    def test_a_payload_without_a_semaphore_is_never_read(self):
        self.take_alias()
        d = self.req_dir()
        (d / "x1.json").write_text(json.dumps(
            {"tool": "mailbox-list", "args": {}}), encoding="utf-8")
        self.core.process_alias("osync")
        self.assertTrue((d / "x1.json").exists())
        self.assertFalse((self.root / "sessions/osync/replies/x1.md").exists())

    def test_writing_is_left_alone_until_ready(self):
        self.take_alias()
        self.write_request("x2", "mailbox-list", status="writing")
        self.core.process_alias("osync")
        d = self.req_dir()
        self.assertTrue((d / "x2.json").exists())
        self.assertTrue((d / "x2.sem").exists())
        self.assertIn(("osync", "x2"), self.core._pending)
        (d / "x2.sem").write_text(json.dumps(
            {"op": "mailbox-list", "status": "ready"}), encoding="utf-8")
        self.core.process_alias("osync", {"x2"})
        self.assertFalse((d / "x2.json").exists())
        self.assertFalse((d / "x2.sem").exists())
        self.assertIn("No messages", self.reply("x2"))

    def test_ready_with_a_size_mismatch_waits_for_the_sync(self):
        self.take_alias()
        d = self.req_dir()
        self.write_request("x3", "mailbox-list")
        (d / "x3.sem").write_text(json.dumps(
            {"op": "mailbox-list", "status": "ready", "bytes": 9999}), encoding="utf-8")
        self.core.process_alias("osync")
        self.assertTrue((d / "x3.json").exists())
        self.assertIn(("osync", "x3"), self.core._pending)

    def test_ready_with_the_right_size_is_taken_and_both_files_deleted(self):
        self.take_alias()
        self.write_request("x4", "mailbox-sessions", with_bytes=True)
        self.core.process_alias("osync")
        d = self.req_dir()
        self.assertEqual(list(d.iterdir()), [])
        self.assertIn("osync", self.reply("x4"))

    def test_cancelled_deletes_both_and_does_nothing(self):
        self.take_alias()
        self.write_request("x5", "mailbox-send", {
            "to": "nobody", "subject": "s", "body": "b"}, status="cancelled")
        self.core.process_alias("osync")
        self.assertEqual(list(self.req_dir().iterdir()), [])
        self.assertFalse(
            (self.root / "sessions/osync/replies/x5.md").exists())
        self.assertEqual(self.store.sent(from_alias="osync",
                                         from_host="cloud"), [])

    def test_a_writer_that_never_finishes_is_rejected_after_the_timeout(self):
        self.take_alias()
        self.write_request("x6", "mailbox-list", status="writing")
        self.core.process_alias("osync")
        self.clock.t += 3601
        self.core.tick()
        rej = self.root / "sessions/osync/rejected"
        self.assertTrue((rej / "x6.json").exists())
        self.assertIn("still `writing`",
                      (rej / "x6.reason.txt").read_text(encoding="utf-8"))
        self.assertIn("REJECTED", self.reply("x6"))
        self.assertNotIn(("osync", "x6"), self.core._pending)

    def test_an_unparseable_semaphore_is_retried_then_rejected(self):
        self.take_alias()
        d = self.req_dir()
        (d / "x7.json").write_text(json.dumps(
            {"tool": "mailbox-list", "args": {}}), encoding="utf-8")
        (d / "x7.sem").write_text('{"op": "mailbox-list", "stat', encoding="utf-8")
        self.core.process_alias("osync")
        self.assertTrue((d / "x7.json").exists())
        self.clock.t += 3601
        self.core.tick()
        self.assertIn("never became valid", self.reply("x7"))

    def test_op_and_tool_must_agree(self):
        self.take_alias()
        self.write_request("x8", "mailbox-read", {"ids": [1]},
                           op="mailbox-list")
        self.core.process_alias("osync")
        self.assertIn("REJECTED", self.reply("x8"))
        self.assertIn("semaphore says op", self.reply("x8"))

    def test_an_unknown_tool_is_rejected(self):
        self.take_alias()
        self.write_request("x9", "mailbox-delete-everything")
        self.core.process_alias("osync")
        self.assertIn("unknown tool", self.reply("x9"))

    def test_the_daemon_writes_its_semaphore_last(self):
        order = []
        real = relay._write_text

        def spy(path, text):
            order.append((Path(path).name, json.loads(text)["status"]
                          if Path(path).suffix == ".sem" else "payload"))
            return real(path, text)

        with mock.patch.object(relay, "_write_text", spy):
            write_pair(self.tmp, "r1", ".md", "hello", "mailbox-list")
        self.assertEqual(order, [("r1.sem", "writing"), ("r1.md", "payload"),
                                 ("r1.sem", "ready")])
        self.assertEqual(read_semaphore(self.tmp / "r1.sem").bytes, 5)


class AliasTests(RelayHarness):

    def test_requests_before_an_alias_are_refused(self):
        self.write_request("a1", "mailbox-list")
        self.core.process_alias("osync")
        self.assertIn("has no alias yet", self.reply("a1"))

    def test_taking_an_alias_makes_the_session_alias_at_cloud(self):
        text = self.take_alias()
        self.assertIn("`osync@cloud`", text)
        rows = [(s.alias, s.host, s.session_id)
                for s in self.store.sessions()]
        self.assertIn(("osync", "cloud", "cloud-osync"), rows)
        status = json.loads(
            (self.root / "sessions/osync/status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["address"], "osync@cloud")

    def test_the_alias_must_match_the_folder(self):
        self.write_request("alias", ALIAS_OP, {"alias": "other"})
        self.core.process_alias("osync")
        self.assertIn("folder name is the alias", self.reply("alias"))
        self.assertFalse(self.core.registered("osync"))

    def test_an_allow_list_is_enforced(self):
        self.core = self.make_core(aliases=["xollama"])
        text = self.take_alias("osync")
        self.assertIn("allow-list", text)

    def test_the_alias_request_runs_before_requests_that_arrived_with_it(self):
        self.write_request("b1", "mailbox-sessions")
        os.utime(self.req_dir() / "b1.sem", (1, 1))     # older
        self.write_request("alias", ALIAS_OP, {"alias": "osync"})
        self.core.process_alias("osync")
        self.assertNotIn("no alias yet", self.reply("b1"))

    def test_a_new_cloud_session_takes_over_with_its_mail(self):
        self.take_alias()
        self.local().call("mailbox-send", {
            "to": "osync@cloud", "subject": "waiting", "body": "for you"},
            headless=False)
        self.core = self.make_core()            # a new relay, same folder
        self.take_alias()
        self.write_request("l1", "mailbox-list")
        self.core.process_alias("osync")
        self.assertIn("waiting", self.reply("l1"))

    def test_a_bare_alias_on_two_hosts_is_flagged_on_registration(self):
        self.store.register("osync-solidpc", "osync", host="solidpc")
        text = self.take_alias()
        self.assertIn("osync@solidpc", text)


class RoundTripTests(RelayHarness):
    """Every tool, through the folder, against the real mailbox."""

    def setUp(self):
        super().setUp()
        self.take_alias()
        self.peer = self.local()

    def run_req(self, rid, tool, args=None) -> str:
        self.write_request(rid, tool, args)
        self.core.process_alias("osync")
        return self.reply(rid)

    def test_local_to_cloud_lands_in_the_inbox_summary_and_reads(self):
        self.peer.call("mailbox-send", {
            "to": "osync@cloud", "subject": "hello cloud",
            "body": "the body"}, headless=False)
        self.core.tick()
        inbox = (self.root / "sessions/osync/INBOX.md").read_text(encoding="utf-8")
        self.assertIn("hello cloud", inbox)
        self.assertIn("Unread: 1", inbox)
        self.assertEqual(read_semaphore(
            self.root / "sessions/osync/INBOX.sem").status, "ready")
        mid = self.store.inbox(alias="osync", host="cloud")[0]["id"]
        self.assertIn("the body", self.run_req("r1", "mailbox-read",
                                               {"ids": [mid]}))
        self.core.tick()
        self.assertIn("Unread: 0", (self.root
                                    / "sessions/osync/INBOX.md").read_text(encoding="utf-8"))

    def test_cloud_to_local_is_from_alias_at_cloud(self):
        self.run_req("s1", "mailbox-send", {
            "to": "claude-hooks@solidpc", "subject": "from the cloud",
            "body": "hi"})
        row = self.store.inbox(alias="claude-hooks", host="solidpc")[0]
        self.assertEqual((row["from_alias"], row["from_host"]),
                         ("osync", "cloud"))
        self.assertIn("from the cloud", self.run_req("s2", "mailbox-sent"))

    def test_ack_edit_cancel_and_sessions(self):
        self.peer.call("mailbox-send", {
            "to": "osync@cloud", "subject": "q", "body": "?"},
            headless=False)
        mid = self.store.inbox(alias="osync", host="cloud")[0]["id"]
        self.run_req("k0", "mailbox-read", {"ids": [mid]})
        self.assertNotIn("REJECTED", self.run_req(
            "k1", "mailbox-ack", {"id": mid, "note": "on it"}))
        self.run_req("k2", "mailbox-send", {
            "to": "claude-hooks@solidpc", "subject": "draft", "body": "v1"})
        sent = self.store.sent(from_alias="osync", from_host="cloud")[0]
        self.run_req("k3", "mailbox-edit", {"id": sent["id"], "body": "v2"})
        self.assertEqual(self.store.sent(from_alias="osync",
                                         from_host="cloud")[0]["body"], "v2")
        self.run_req("k4", "mailbox-cancel", {"id": sent["id"]})
        self.assertIn("claude-hooks", self.run_req("k5", "mailbox-sessions"))

    def test_the_cloud_cannot_withdraw_solidpc_mail(self):
        self.peer.call("mailbox-send", {
            "to": "osync@cloud", "subject": "mine", "body": "not yours"},
            headless=False)
        mid = self.store.sent(from_alias="claude-hooks",
                              from_host="solidpc")[0]["id"]
        self.run_req("c1", "mailbox-cancel", {"id": mid})
        still = self.store.sent(from_alias="claude-hooks",
                                from_host="solidpc")[0]
        self.assertIsNone(still["cancelled_at"])


class CostTests(RelayHarness):

    def test_an_empty_folder_opens_no_database_and_sets_no_timer(self):
        core = self.make_core()
        core.full_scan()
        self.assertEqual(self.factory_calls, 0)
        self.assertIsNone(core.next_wakeup())

    def test_a_live_session_earns_a_timer_until_the_window_passes(self):
        self.take_alias()
        self.assertEqual(self.core.next_wakeup(), 30.0)
        self.clock.t += 12 * 3600 + 1
        self.assertIsNone(self.core.next_wakeup())

    def test_a_request_waiting_for_its_writer_earns_a_timer(self):
        core = self.make_core()
        self.write_request("w1", "mailbox-list", status="writing")
        core.full_scan()
        self.assertEqual(core.next_wakeup(), 30.0)

    def test_the_inbox_summary_is_rewritten_only_when_it_changes(self):
        self.take_alias()
        self.core.tick()
        inbox = self.root / "sessions/osync/INBOX.md"
        os.utime(inbox, (1, 1))
        self.core.tick()
        self.assertEqual(inbox.stat().st_mtime, 1)

    def test_the_interval_cannot_go_below_30_seconds(self):
        o = settings({"hooks": {"mailbox": {"enabled": True, "cloud_relay": {
            "enabled": True, "root": "/x", "interval_seconds": 1}}}})
        self.assertEqual(o["interval"], 30.0)

    def test_disabled_unless_the_mailbox_and_a_root_are_set(self):
        self.assertFalse(settings({"hooks": {"mailbox": {
            "enabled": False, "cloud_relay": {"enabled": True,
                                              "root": "/x"}}}})["enabled"])
        self.assertFalse(settings({"hooks": {"mailbox": {
            "enabled": True, "cloud_relay": {"enabled": True}}}})["enabled"])
        self.assertIsNone(relay.start_relay_thread({}, threading.Event()))

    def test_idle_session_folders_are_archived_not_deleted(self):
        self.take_alias()
        self.clock.t += 31 * 86400
        moved = self.core.archive_idle()
        self.assertEqual(moved, ["osync"])
        archived = list((self.root / "archive").iterdir())
        self.assertEqual(len(archived), 1)
        self.assertTrue((archived[0] / "status.json").exists())


class _ScriptedWatcher:
    """Returns scripted change sets; a wait with nothing scripted
    advances the fake monotonic clock by its timeout (or stops the
    loop when the relay would block forever)."""

    kind = "scripted"

    def __init__(self, clock, script, stop):
        self.clock, self.script, self.stop = clock, list(script), stop
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.script:
            delay, changes = self.script.pop(0)
            self.clock.t += delay
            return set(changes)
        if timeout is None:
            self.stop.set()
            return set()
        self.clock.t += timeout
        return set()

    def wake(self):
        pass

    def close(self):
        pass


class LoopBatchingTests(RelayHarness):

    def run_loop(self, script):
        mono = Clock(1000.0)
        stop = threading.Event()
        thread = MailboxRelayThread(
            {"hooks": {"mailbox": {"enabled": True, "cloud_relay": {
                "enabled": True, "root": str(self.root)}}}},
            stop_event=stop)
        thread.core = self.core
        thread.watcher = _ScriptedWatcher(mono, script, stop)
        passes = []
        real = self.core.handle_changes

        def counted(changes):
            passes.append((mono.t, set(changes)))
            return real(changes)

        with mock.patch.object(self.core, "handle_changes", counted), \
             mock.patch.object(relay.time, "monotonic", mono):
            thread.loop()
        return passes, thread.watcher

    def test_idle_blocks_forever_with_no_timer(self):
        passes, w = self.run_loop([])
        self.assertEqual(passes, [])
        self.assertEqual(w.waits, [None])

    def test_events_within_the_interval_are_batched_into_one_pass(self):
        ev = lambda rid: ("sessions", "osync", "requests", f"{rid}.sem")
        passes, _ = self.run_loop([
            (40, [ev("a")]),      # first event after idle: immediate pass
            (1, [ev("b")]),       # 1 s later: waits for the interval...
            (2, [ev("c")]),       # ...and this joins the same pass
        ])
        self.assertEqual(len(passes), 2)
        self.assertEqual(passes[1][1], {ev("b"), ev("c")})
        self.assertGreaterEqual(passes[1][0] - passes[0][0], 30)


class WatcherTests(unittest.TestCase):

    def test_classify_relative(self):
        c = watch.classify_relative
        self.assertEqual(c("sessions\\osync\\requests\\a.sem"),
                         ("sessions", "osync", "requests", "a.sem"))
        self.assertEqual(c("sessions/osync"), ("sessions", "osync"))
        self.assertEqual(c("sessions/osync/requests"),
                         ("sessions", "osync", "requests"))
        self.assertIsNone(c("sessions/osync/replies/a.md"))
        self.assertIsNone(c("sessions/osync/INBOX.md"))
        self.assertEqual(c("sessions"), watch.RESCAN)

    def test_parse_notify_buffer(self):
        import struct
        n1 = "sessions\\a\\requests\\x.sem".encode("utf-16-le")
        n2 = "MAILBOX.md".encode("utf-16-le")
        rec1 = struct.pack("<III", 0, 3, len(n1)) + n1
        pad = (-len(rec1)) % 4
        rec1 = struct.pack("<III", len(rec1) + pad, 3, len(n1)) + n1 \
            + b"\0" * pad
        rec2 = struct.pack("<III", 0, 1, len(n2)) + n2
        self.assertEqual(watch.parse_notify_buffer(rec1 + rec2),
                         ["sessions\\a\\requests\\x.sem", "MAILBOX.md"])

    def test_no_native_watcher_means_polling(self):
        with mock.patch.object(watch, "inotify_available", lambda: False), \
             mock.patch.object(watch.sys, "platform", "darwin"), \
             tempfile.TemporaryDirectory() as d:
            w = watch.make_watcher(Path(d), "auto", poll_interval=30)
            self.assertEqual(w.kind, "poll")

    def test_poll_watcher_sees_a_new_semaphore(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            req = root / "sessions" / "osync" / "requests"
            req.mkdir(parents=True)
            w = watch.PollWatcher(root, interval=1)
            (req / "a.sem").write_text("{}", encoding="utf-8")
            self.assertIn(("sessions", "osync", "requests", "a.sem"),
                          w.wait(0.01))

    def _wait_for(self, w, target, seconds=5.0):
        seen, deadline = set(), time.monotonic() + seconds
        while target not in seen and time.monotonic() < deadline:
            seen |= w.wait(0.5)
        return seen

    @unittest.skipUnless(sys.platform.startswith("linux")
                         and watch.inotify_available(), "inotify")
    def test_inotify_reports_a_finished_semaphore_and_not_our_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            req = root / "sessions" / "osync" / "requests"
            req.mkdir(parents=True)
            w = watch.make_watcher(root)
            self.assertEqual(w.kind, "inotify")
            try:
                (req / "a.sem").write_text("{}", encoding="utf-8")
                target = ("sessions", "osync", "requests", "a.sem")
                self.assertIn(target, self._wait_for(w, target))
                # The daemon's own writes: a replies/ dir and a file in
                # it, and a rewrite of a top-level file. None of them is
                # a change the relay acts on.
                (root / "sessions" / "osync" / "replies").mkdir()
                (root / "sessions" / "osync" / "replies" / "x.md"
                 ).write_text("x", encoding="utf-8")
                (root / "sessions" / "osync" / "INBOX.md").write_text("x", encoding="utf-8")
                (root / "sessions" / "osync" / "INBOX.md").write_text("y", encoding="utf-8")
                self.assertEqual(w.wait(0.2), set())
            finally:
                w.close()

    @unittest.skipUnless(sys.platform.startswith("linux")
                         and watch.inotify_available(), "inotify")
    def test_inotify_reports_structure_created_after_it_started(self):
        """A tree created after the watch began may hold files written
        before their directory was watched, so it is reported for a
        rescan — and files written after that are seen directly."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            w = watch.make_watcher(root)
            try:
                req = root / "sessions" / "osync" / "requests"
                req.mkdir(parents=True)
                seen = self._wait_for(w, watch.RESCAN, 2.0)
                self.assertIn(watch.RESCAN, seen)
                # Drain whatever else the mkdir -p produced.
                w.wait(0.2)
                (req / "b.sem").write_text("{}", encoding="utf-8")
                target = ("sessions", "osync", "requests", "b.sem")
                self.assertIn(target, self._wait_for(w, target))
            finally:
                w.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows_watcher_reports_a_semaphore(self):  # pragma: no cover
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            req = root / "sessions" / "osync" / "requests"
            req.mkdir(parents=True)
            w = watch.make_watcher(root)
            self.assertEqual(w.kind, "windows")
            try:
                (req / "a.sem").write_text("{}", encoding="utf-8")
                seen = set()
                deadline = time.monotonic() + 5
                target = ("sessions", "osync", "requests", "a.sem")
                while target not in seen and time.monotonic() < deadline:
                    seen |= w.wait(1.0)
                self.assertIn(target, seen)
            finally:
                w.close()


class InstructionsTests(unittest.TestCase):

    def test_install_then_current_then_updated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(install_instructions(root), "installed")
            self.assertEqual(install_instructions(root), "current")
            (root / "MAILBOX.md").write_text("old", encoding="utf-8")
            self.assertEqual(install_instructions(root), "updated")
            self.assertTrue((root / "sessions").is_dir())

    def test_the_relay_reports_missing_and_stale_instructions(self):
        with tempfile.TemporaryDirectory() as d:
            core = RelayCore(Path(d), opts(), store_factory=lambda: None)
            self.assertEqual(core.check_instructions(), "missing")
            (Path(d) / "MAILBOX.md").write_text("old", encoding="utf-8")
            self.assertEqual(core.check_instructions(), "stale")
            install_instructions(Path(d))
            self.assertEqual(core.check_instructions(), "current")

    def test_every_tool_is_documented_for_the_cloud_session(self):
        from claude_hooks.mailbox.tools import TOOL_NAMES
        text = relay.instructions_source().read_text(encoding="utf-8")
        for name in (*TOOL_NAMES, ALIAS_OP):
            self.assertIn(f"`{name}`", text, name)
        for status in ("writing", "ready", "cancelled"):
            self.assertIn(f'"{status}"', text)


if __name__ == "__main__":
    unittest.main()


class InstallerTests(unittest.TestCase):
    """install._setup_mailbox_cloud_relay: defaults from the current
    config, and the instructions installed whenever the relay is on."""

    def setUp(self):
        repo = Path(__file__).resolve().parent.parent
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        import install
        self.install = install
        # Never touch this host's real daemon unit from a test.
        p = mock.patch.object(relay, "daemon_unit_paths", return_value=[])
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "mailbox"

    def cfg(self, enabled=True):
        return {"hooks": {"mailbox": {"enabled": True, "cloud_relay": {
            "enabled": enabled, "root": str(self.root)}}}}

    def test_non_interactive_keeps_config_and_installs_instructions(self):
        cfg = self.cfg()
        self.install._setup_mailbox_cloud_relay(
            cfg, non_interactive=True, dry_run=False)
        self.assertTrue(cfg["hooks"]["mailbox"]["cloud_relay"]["enabled"])
        self.assertEqual((self.root / "MAILBOX.md").read_bytes(),
                         relay.instructions_source().read_bytes())

    def test_dry_run_writes_nothing(self):
        self.install._setup_mailbox_cloud_relay(
            self.cfg(), non_interactive=True, dry_run=True)
        self.assertFalse((self.root / "MAILBOX.md").exists())

    def test_empty_answers_keep_the_current_setting(self):
        cfg = self.cfg()
        with mock.patch("builtins.input", return_value=""):
            self.install._setup_mailbox_cloud_relay(
                cfg, non_interactive=False, dry_run=False)
        section = cfg["hooks"]["mailbox"]["cloud_relay"]
        self.assertTrue(section["enabled"])
        self.assertEqual(section["root"], str(self.root))

    def test_off_stays_off_and_installs_nothing(self):
        cfg = self.cfg(enabled=False)
        with mock.patch("builtins.input", return_value=""):
            self.install._setup_mailbox_cloud_relay(
                cfg, non_interactive=False, dry_run=False)
        self.assertFalse(cfg["hooks"]["mailbox"]["cloud_relay"]["enabled"])
        self.assertFalse((self.root / "MAILBOX.md").exists())


class SandboxGrantTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.unit = self.tmp / "claude-hooks-daemon.service"
        self.unit.write_text("[Service]\nProtectSystem=strict\n"
                             "ReadWritePaths=/root/.claude\n", encoding="utf-8")
        self.real = self.tmp / "real-mailbox"
        self.real.mkdir()
        self.link = self.tmp / "mailbox"
        self.link.symlink_to(self.real)

    def test_both_spellings_of_a_symlinked_folder_are_needed(self):
        self.assertEqual(relay.relay_rw_paths(self.link),
                         [str(self.link), str(self.real.resolve())])
        self.assertEqual(relay.missing_grants(self.unit, self.link),
                         [str(self.link), str(self.real.resolve())])

    def test_an_unsandboxed_unit_needs_nothing(self):
        self.unit.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
        self.assertEqual(relay.missing_grants(self.unit, self.link), [])

    def test_the_dropin_grants_and_a_parent_grant_counts(self):
        with mock.patch.object(relay, "daemon_unit_paths",
                               return_value=[(self.unit, "system")]), \
             mock.patch("subprocess.run") as run:
            notes = relay.ensure_unit_grant(self.link)
        self.assertEqual(len(notes), 1)
        run.assert_called_once()
        self.assertEqual(relay.missing_grants(self.unit, self.link), [])
        dropin = Path(f"{self.unit}.d") / relay.GRANT_DROPIN
        self.assertIn(f"-{self.link}", dropin.read_text(encoding="utf-8"))
        dropin.write_text(f"[Service]\nReadWritePaths={self.tmp}\n", encoding="utf-8")
        self.assertEqual(relay.missing_grants(self.unit, self.link), [])
        # Already granted: nothing written, nothing reloaded.
        with mock.patch.object(relay, "daemon_unit_paths",
                               return_value=[(self.unit, "system")]), \
             mock.patch("subprocess.run") as run:
            self.assertEqual(relay.ensure_unit_grant(self.link), [])
        run.assert_not_called()

    def test_the_write_probe(self):
        (self.real / "sessions").mkdir()
        self.assertIsNone(relay.writable_problem(self.real))
        broken = self.tmp / "broken"
        broken.mkdir()
        (broken / "sessions").write_text("not a directory", encoding="utf-8")
        with mock.patch.object(relay, "daemon_unit_paths", return_value=[]):
            self.assertIn("cannot write", relay.writable_problem(broken))


class UndeletableRequestTests(RelayHarness):

    def test_a_request_that_cannot_be_removed_is_not_run(self):
        self.take_alias()
        self.write_request("u1", "mailbox-send", {
            "to": "osync@cloud", "subject": "once", "body": "only once"})
        real = relay._unlink

        def refuse(path):
            if Path(path).name.startswith("u1."):
                raise OSError(30, "Read-only file system")
            return real(path)

        with mock.patch.object(relay, "_unlink", refuse):
            self.core.process_alias("osync")
        self.assertEqual(self.store.sent(from_alias="osync",
                                         from_host="cloud"), [])
        self.assertIn(("osync", "u1"), self.core._pending)
        self.core.process_alias("osync")          # now removable
        self.assertEqual(len(self.store.sent(from_alias="osync",
                                             from_host="cloud")), 1)

    def test_one_failing_request_does_not_stop_the_others(self):
        self.take_alias()
        self.write_request("f1", "mailbox-sessions")
        self.write_request("f2", "mailbox-sessions")
        real = self.core._process_request

        def flaky(alias, rid):
            if rid == "f1":
                raise OSError(5, "I/O error")
            return real(alias, rid)

        with mock.patch.object(self.core, "_process_request", flaky):
            self.core.process_alias("osync")
        self.assertIn("osync", self.reply("f2"))
