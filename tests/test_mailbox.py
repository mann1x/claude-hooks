"""Mailbox: addressing, storage, and the ownership rules.

The store tests run against a real SQLite file rather than a mock, so
the SQL is actually executed — including the two CHECK constraints,
which are the only invariants worth enforcing in the database.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.mailbox import (  # noqa: E402
    Address, AddressError, MailboxError, MailboxStore, Session,
    describe_recipients, parse_address, resolve,
)
from claude_hooks.mailbox.store import utcnow  # noqa: E402


def sess(alias, host, sid=None, os_name="linux"):
    return Session(session_id=sid or f"{alias}-{host}", alias=alias,
                   host=host, os=os_name)


class ParseTests(unittest.TestCase):

    def test_bare_alias(self):
        a = parse_address("osync")
        self.assertEqual(a.alias, "osync")
        self.assertIsNone(a.host)
        self.assertFalse(a.broadcast)

    def test_host_qualified(self):
        a = parse_address("osync@solidpc")
        self.assertEqual((a.alias, a.host), ("osync", "solidpc"))

    def test_broadcast(self):
        a = parse_address("osync*")
        self.assertTrue(a.broadcast)
        self.assertEqual(a.alias, "osync")

    def test_session_id(self):
        a = parse_address("77aee209-0dfe-44b9-b819-bfe3e328dc33")
        self.assertTrue(a.is_session)
        self.assertIsNone(a.alias)

    def test_session_id_is_lowercased(self):
        a = parse_address("77AEE209-0DFE-44B9-B819-BFE3E328DC33")
        self.assertEqual(a.session_id, "77aee209-0dfe-44b9-b819-bfe3e328dc33")

    def test_host_and_broadcast_together_is_refused(self):
        """Both readings are plausible, so guessing is the mistake this
        module exists to avoid."""
        with self.assertRaises(AddressError) as cm:
            parse_address("osync@solidpc*")
        self.assertIn("Pick one", str(cm.exception))

    def test_trailing_at_is_refused(self):
        with self.assertRaises(AddressError):
            parse_address("osync@")

    def test_bare_star_is_refused(self):
        """Broadcast is per-alias; there is deliberately no way to
        message every session on the machine."""
        with self.assertRaises(AddressError) as cm:
            parse_address("*")
        self.assertIn("per-alias", str(cm.exception))

    def test_empty_and_whitespace(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                with self.assertRaises(AddressError):
                    parse_address(raw)   # type: ignore[arg-type]

    def test_invalid_characters(self):
        for raw in ("os ync", "os/ync", "os:ync"):
            with self.subTest(raw=raw):
                with self.assertRaises(AddressError):
                    parse_address(raw)

    def test_str_round_trips(self):
        for raw in ("osync", "osync@solidpc", "osync*"):
            with self.subTest(raw=raw):
                self.assertEqual(str(parse_address(raw)), raw)


class ResolveTests(unittest.TestCase):

    def setUp(self):
        self.registry = [sess("osync", "solidpc"), sess("osync", "pandorum"),
                         sess("claude-hooks", "solidpc")]

    def test_unambiguous_bare_alias_sends(self):
        """Cross-host traffic is rare; taxing every send with @host to
        guard against it is the wrong default."""
        got = resolve(parse_address("claude-hooks"), self.registry)
        self.assertEqual(len(got), 1)

    def test_ambiguous_bare_alias_is_refused_with_candidates(self):
        with self.assertRaises(AddressError) as cm:
            resolve(parse_address("osync"), self.registry)
        msg = str(cm.exception)
        self.assertIn("Nothing was sent", msg)
        self.assertIn("osync@solidpc", msg)
        self.assertIn("osync@pandorum", msg)
        self.assertIn("osync*", msg)

    def test_host_qualified_picks_one(self):
        got = resolve(parse_address("osync@pandorum"), self.registry)
        self.assertEqual([s.host for s in got], ["pandorum"])

    def test_broadcast_takes_all(self):
        got = resolve(parse_address("osync*"), self.registry)
        self.assertEqual(sorted(s.host for s in got), ["pandorum", "solidpc"])

    def test_unknown_alias_is_empty_not_an_error(self):
        """Mail waits. The session you have something for is usually the
        one that is closed."""
        self.assertEqual(resolve(parse_address("nobody"), self.registry), [])

    def test_unknown_host_for_a_known_alias_is_empty(self):
        self.assertEqual(
            resolve(parse_address("osync@mars"), self.registry), [])

    def test_duplicate_session_id_is_a_loud_refusal(self):
        dupes = [sess("a", "h1", sid="dup"), sess("a", "h2", sid="dup")]
        with self.assertRaises(AddressError) as cm:
            resolve(Address(session_id="dup"), dupes)
        self.assertIn("registry bug", str(cm.exception))

    def test_describe_names_the_recipients(self):
        addr = parse_address("osync*")
        text = describe_recipients(resolve(addr, self.registry), addr)
        self.assertIn("osync@solidpc", text)
        self.assertIn("osync@pandorum", text)

    def test_describe_warns_when_nothing_matched(self):
        addr = parse_address("typo")
        text = describe_recipients([], addr)
        self.assertIn("waits", text)
        self.assertIn("mailbox-sessions", text)


class _SqliteConn:
    """Minimal provider stand-in: one sqlite connection plus a lock,
    which is exactly what MailboxStore borrows from pgvector."""

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


class StoreHarness(unittest.TestCase):
    """Shared SQLite-backed harness with the host name **pinned**.

    ``send()`` stamps ``host_name()`` on every row, and the sender-side
    queries match on it. Tests that name a host literally therefore only
    pass on a machine that happens to be called that — which is why the
    tools tests passed on solidpc and failed on pandorum. Pinning it
    here makes the whole file host-independent; a test that wants to
    *be* another host patches over this for the duration of its send.
    """

    HOST = "solidpc"

    def setUp(self):
        from unittest import mock
        from claude_hooks.mailbox import store as store_mod
        patcher = mock.patch.object(store_mod, "host_name",
                                    lambda: self.HOST)
        patcher.start()
        self.addCleanup(patcher.stop)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = _SqliteConn(Path(self._tmp.name) / "m.db")
        # Registered *after* the tempdir cleanup so it runs *before* it:
        # addCleanup is LIFO, and Windows refuses to delete a file that
        # is still open. On POSIX the unlink succeeds either way, which
        # is why this only ever fails on pandorum.
        self.addCleanup(self.db.close)
        self.store = MailboxStore(self.db, self.db.lock, dialect="sqlite")
        self.store.ensure_schema()

    def register(self, alias, host, sid=None):
        return self.store.register(sid or f"{alias}-{host}", alias, host=host)


class RegistryTests(StoreHarness):

    def test_register_and_list(self):
        self.register("osync", "solidpc")
        got = self.store.sessions()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].alias, "osync")

    def test_registering_reports_alias_collisions(self):
        """The only place a sender can later be surprised by it."""
        self.register("osync", "solidpc")
        others = self.register("osync", "pandorum")
        self.assertEqual([o.host for o in others], ["solidpc"])

    def test_no_collision_reported_for_a_unique_alias(self):
        self.assertEqual(self.register("solo", "solidpc"), [])

    def test_re_registering_the_same_session_is_idempotent(self):
        self.register("osync", "solidpc", sid="s1")
        self.register("osync", "solidpc", sid="s1")
        self.assertEqual(len(self.store.sessions()), 1)

    def test_os_is_recorded_for_run_this_on_windows(self):
        self.register("osync", "solidpc")
        self.assertTrue(self.store.sessions()[0].os)

    def test_sweep_forgets_stale_sessions(self):
        self.register("old", "h1")
        cutoff = utcnow() - timedelta(days=40)
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE session_registry SET last_seen = ?",
                (cutoff.isoformat(),))
            self.db.conn.commit()
        self.assertEqual(self.store.sweep_registry(days=30), 1)
        self.assertEqual(self.store.sessions(), [])

    def test_sweep_keeps_fresh_sessions(self):
        self.register("new", "h1")
        self.assertEqual(self.store.sweep_registry(days=30), 0)


class SendTests(StoreHarness):

    def test_send_to_a_registered_alias(self):
        self.register("osync", "solidpc")
        res = self.store.send("osync", "subject", "body", from_alias="me")
        self.assertEqual(len(res["ids"]), 1)
        self.assertEqual(len(res["recipients"]), 1)

    def test_send_to_an_unregistered_alias_still_queues(self):
        res = self.store.send("ghost", "s", "b", from_alias="me")
        self.assertEqual(len(res["ids"]), 1)
        self.assertEqual(res["recipients"], [])

    def test_ambiguous_send_writes_nothing(self):
        """Resolution happens before any insert, so a refusal leaves no
        partial delivery."""
        self.register("osync", "solidpc")
        self.register("osync", "pandorum")
        with self.assertRaises(AddressError):
            self.store.send("osync", "s", "b", from_alias="me")
        self.assertEqual(self.store.sent(from_alias="me"), [])

    def test_broadcast_writes_one_row_per_host_with_a_shared_group(self):
        self.register("osync", "solidpc")
        self.register("osync", "pandorum")
        res = self.store.send("osync*", "s", "b", from_alias="me")
        self.assertEqual(len(res["ids"]), 2)
        self.assertIsNotNone(res["broadcast_group"])

    def test_empty_subject_or_body_refused(self):
        for kwargs in ({"subject": "", "body": "b"},
                       {"subject": "s", "body": "  "}):
            with self.subTest(**kwargs):
                with self.assertRaises(MailboxError):
                    self.store.send("x", kwargs["subject"], kwargs["body"],
                                    from_alias="me")

    def test_priority_is_stored(self):
        self.store.send("x", "s", "b", from_alias="me", priority=2)
        self.assertEqual(self.store.sent(from_alias="me")[0]["priority"], 2)


class InboxTests(StoreHarness):

    def setUp(self):
        super().setUp()
        self.register("me", "solidpc", sid="mine")

    def test_unread_only_by_default(self):
        self.store.send("me", "s1", "b1", from_alias="them")
        box = self.store.inbox(alias="me", session_id="mine", host="solidpc")
        self.assertEqual(len(box), 1)
        self.store.read([box[0]["id"]], reader_session="mine", alias="me",
                        host="solidpc")
        self.assertEqual(
            self.store.inbox(alias="me", session_id="mine", host="solidpc"), [])

    def test_include_read(self):
        self.store.send("me", "s", "b", from_alias="them")
        box = self.store.inbox(alias="me", session_id="mine", host="solidpc")
        self.store.read([box[0]["id"]], reader_session="mine", alias="me",
                        host="solidpc")
        self.assertEqual(len(self.store.inbox(
            alias="me", session_id="mine", host="solidpc",
            include_read=True)), 1)

    def test_high_priority_sorts_first(self):
        self.store.send("me", "low", "b", from_alias="t", priority=0)
        self.store.send("me", "high", "b", from_alias="t", priority=5)
        box = self.store.inbox(alias="me", session_id="mine", host="solidpc")
        self.assertEqual(box[0]["subject"], "high")

    def test_host_qualified_mail_is_not_visible_on_another_host(self):
        self.register("me", "pandorum", sid="other")
        self.store.send("me@pandorum", "s", "b", from_alias="them")
        self.assertEqual(self.store.inbox(alias="me", session_id="mine",
                                          host="solidpc"), [])
        self.assertEqual(len(self.store.inbox(alias="me", session_id="other",
                                              host="pandorum")), 1)

    def test_since_filters_to_mid_turn_arrivals(self):
        """What the Stop hook uses so it never repeats what
        UserPromptSubmit already showed."""
        self.store.send("me", "before", "b", from_alias="t")
        cut = utcnow()
        import time
        time.sleep(0.01)
        self.store.send("me", "after", "b", from_alias="t")
        box = self.store.inbox(alias="me", session_id="mine", host="solidpc",
                               since=cut)
        self.assertEqual([m["subject"] for m in box], ["after"])

    def test_cancelled_messages_are_invisible(self):
        self.store.send("me", "s", "b", from_alias="them")
        mid = self.store.sent(from_alias="them")[0]["id"]
        self.store.cancel(mid, from_alias="them")
        self.assertEqual(self.store.inbox(alias="me", session_id="mine",
                                          host="solidpc"), [])


class ReadTests(StoreHarness):

    def setUp(self):
        super().setUp()
        self.register("me", "solidpc", sid="mine")
        self.store.send("me", "s", "the body", from_alias="them")
        self.mid = self.store.sent(from_alias="them")[0]["id"]

    def test_read_returns_the_body_and_stamps(self):
        rows = self.store.read([self.mid], reader_session="mine", alias="me",
                               host="solidpc")
        self.assertEqual(rows[0]["body"], "the body")
        self.assertTrue(rows[0]["read_at"])
        self.assertEqual(rows[0]["read_by"], "mine")

    def test_reading_twice_keeps_the_first_reader(self):
        self.store.read([self.mid], reader_session="mine", alias="me",
                        host="solidpc")
        rows = self.store.read([self.mid], reader_session="other", alias="me",
                               host="solidpc")
        self.assertEqual(rows[0]["read_by"], "mine")

    def test_cannot_read_someone_elses_mail(self):
        rows = self.store.read([self.mid], reader_session="x",
                               alias="stranger", host="elsewhere")
        self.assertEqual(rows, [])
        box = self.store.inbox(alias="me", session_id="mine", host="solidpc")
        self.assertEqual(len(box), 1, "must not have been marked read")

    def test_reading_nothing(self):
        self.assertEqual(self.store.read([], reader_session="mine",
                                         alias="me"), [])


class EditAndCancelTests(StoreHarness):

    def setUp(self):
        super().setUp()
        self.register("me", "solidpc", sid="mine")
        self.store.send("me", "original", "body", from_alias="them")
        self.mid = self.store.sent(from_alias="them")[0]["id"]

    def test_edit_while_unread(self):
        self.store.edit(self.mid, from_alias="them", subject="corrected")
        self.assertEqual(self.store.sent(from_alias="them")[0]["subject"],
                         "corrected")

    def test_edit_stamps_edited_at(self):
        self.store.edit(self.mid, from_alias="them", body="new")
        self.assertTrue(self.store.sent(from_alias="them")[0]["edited_at"])

    def test_edit_refused_after_read_and_names_the_reader(self):
        """The failure this whole design exists to prevent, except now
        it is an error rather than a silent rewrite."""
        self.store.read([self.mid], reader_session="mine", alias="me",
                        host="solidpc")
        with self.assertRaises(MailboxError) as cm:
            self.store.edit(self.mid, from_alias="them", subject="too late")
        msg = str(cm.exception)
        self.assertIn("mine", msg)
        self.assertIn("mailbox-send", msg)

    def test_cannot_edit_another_senders_message(self):
        with self.assertRaises(MailboxError) as cm:
            self.store.edit(self.mid, from_alias="someone_else",
                            subject="hijack")
        self.assertIn("not you", str(cm.exception))

    def test_edit_with_no_fields_is_refused(self):
        with self.assertRaises(MailboxError):
            self.store.edit(self.mid, from_alias="them")

    def test_cancel_while_unread(self):
        self.assertEqual(
            self.store.cancel(self.mid, from_alias="them")["cancelled"], 1)

    def test_cancel_refused_after_read(self):
        self.store.read([self.mid], reader_session="mine", alias="me",
                        host="solidpc")
        with self.assertRaises(MailboxError) as cm:
            self.store.cancel(self.mid, from_alias="them")
        self.assertIn("no longer be withdrawn", str(cm.exception))

    def test_cancel_twice_is_refused(self):
        self.store.cancel(self.mid, from_alias="them")
        with self.assertRaises(MailboxError):
            self.store.cancel(self.mid, from_alias="them")

    def test_broadcast_edit_skips_read_copies(self):
        """A broadcast is one message: one host reading it must not
        freeze the copies nobody has opened."""
        self.register("b", "h1", sid="b1")
        self.register("b", "h2", sid="b2")
        self.store.send("b*", "s", "body", from_alias="them")
        ids = [m["id"] for m in self.store.sent(from_alias="them")
               if m["subject"] == "s"]
        self.store.read([ids[0]], reader_session="b1", alias="b", host="h1")
        res = self.store.edit(ids[0], from_alias="them", subject="v2")
        self.assertEqual(res["updated"], 1, "only the unread copy changes")

    def test_broadcast_frozen_once_every_copy_is_read(self):
        self.register("b", "h1", sid="b1")
        self.register("b", "h2", sid="b2")
        self.store.send("b*", "s", "body", from_alias="them")
        ids = [m["id"] for m in self.store.sent(from_alias="them")
               if m["subject"] == "s"]
        self.store.read([ids[0]], reader_session="b1", alias="b", host="h1")
        self.store.read([ids[1]], reader_session="b2", alias="b", host="h2")
        with self.assertRaises(MailboxError) as cm:
            self.store.edit(ids[0], from_alias="them", subject="v2")
        self.assertIn("Every copy", str(cm.exception))


class AckTests(StoreHarness):
    """The receipt is gated on a note: no ack, no announcement."""

    def setUp(self):
        super().setUp()
        self.register("me", "solidpc", sid="mine")
        self.store.send("me", "s", "b", from_alias="them")
        self.mid = self.store.sent(from_alias="them")[0]["id"]

    def read_it(self):
        self.store.read([self.mid], reader_session="mine", alias="me",
                        host="solidpc")

    def test_read_without_an_ack_announces_nothing(self):
        self.read_it()
        self.assertEqual(self.store.pending_receipts(from_alias="them"), [])

    def test_ack_creates_a_pending_receipt(self):
        self.read_it()
        self.store.ack(self.mid, "confirmed, ~2h", session_id="mine",
                       alias="me", host="solidpc")
        pend = self.store.pending_receipts(from_alias="them")
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["ack_body"], "confirmed, ~2h")

    def test_ack_before_reading_is_refused(self):
        """A receipt for something nobody received."""
        with self.assertRaises(MailboxError) as cm:
            self.store.ack(self.mid, "note", session_id="mine", alias="me",
                           host="solidpc")
        self.assertIn("not been read", str(cm.exception))

    def test_ack_is_not_tied_to_the_moment_of_reading(self):
        """Read, start work, discover it is a day of work, say so then."""
        self.read_it()
        res = self.store.ack(self.mid, "this is a day of work",
                             session_id="mine", alias="me", host="solidpc")
        self.assertEqual(res["action"], "attached")

    def test_ack_can_be_replaced_while_the_receipt_is_unread(self):
        self.read_it()
        self.store.ack(self.mid, "first", session_id="mine", alias="me",
                       host="solidpc")
        res = self.store.ack(self.mid, "second", session_id="mine",
                             alias="me", host="solidpc")
        self.assertEqual(res["action"], "replaced")
        self.assertEqual(
            self.store.pending_receipts(from_alias="them")[0]["ack_body"],
            "second")

    def test_ack_frozen_once_the_sender_has_seen_it(self):
        """Same ownership rule as a message, in the other direction."""
        self.read_it()
        self.store.ack(self.mid, "note", session_id="mine", alias="me",
                       host="solidpc")
        self.store.mark_receipts_seen([self.mid], from_alias="them")
        with self.assertRaises(MailboxError) as cm:
            self.store.ack(self.mid, "changed my mind", session_id="mine",
                           alias="me", host="solidpc")
        self.assertIn("frozen", str(cm.exception))

    def test_marking_seen_clears_the_pending_list(self):
        self.read_it()
        self.store.ack(self.mid, "note", session_id="mine", alias="me",
                       host="solidpc")
        self.assertEqual(
            self.store.mark_receipts_seen([self.mid], from_alias="them"), 1)
        self.assertEqual(self.store.pending_receipts(from_alias="them"), [])

    def test_cannot_ack_someone_elses_message(self):
        self.read_it()
        with self.assertRaises(MailboxError):
            self.store.ack(self.mid, "note", session_id="x", alias="stranger",
                           host="elsewhere")

    def test_empty_note_is_refused(self):
        self.read_it()
        with self.assertRaises(MailboxError):
            self.store.ack(self.mid, "   ", session_id="mine", alias="me",
                           host="solidpc")

    def test_database_rejects_an_ack_without_a_read(self):
        """The one invariant worth enforcing in the schema."""
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.lock:
                self.db.conn.execute(
                    "UPDATE session_messages SET ack_body = 'x' WHERE id = ?",
                    (self.mid,))
                self.db.conn.commit()


class ExpiryTests(StoreHarness):

    def test_expired_rows_are_found(self):
        self.store.send("x", "s", "b", from_alias="me", expires_days=-1)
        self.assertEqual(len(self.store.expired()), 1)

    def test_fresh_rows_are_not(self):
        self.store.send("x", "s", "b", from_alias="me")
        self.assertEqual(self.store.expired(), [])

    def test_default_is_180_days(self):
        from claude_hooks.mailbox import DEFAULT_EXPIRY_DAYS
        self.assertEqual(DEFAULT_EXPIRY_DAYS, 180)

    def test_delete_removes(self):
        self.store.send("x", "s", "b", from_alias="me", expires_days=-1)
        ids = [m["id"] for m in self.store.expired()]
        self.assertEqual(self.store.delete(ids), 1)
        self.assertEqual(self.store.expired(), [])


class SchemaConstraintTests(StoreHarness):

    def test_exactly_one_recipient_column(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.lock:
                self.db.conn.execute(
                    "INSERT INTO session_messages "
                    "(created_at, from_alias, subject, body, expires_at) "
                    "VALUES ('t','me','s','b','t')")
                self.db.conn.commit()


class OneMailboxPerAliasTests(StoreHarness):
    """A registration is not an addressee.

    Reported live on 2026-09-18: one `mailbox-send` call was delivered
    **eleven times**. `xollama@solidpc` had eleven registrations — one
    live session plus ten from sessions that had ended minutes apart —
    and `send()` wrote one row per registration. The rows carry
    `to_alias` and `to_host` and nothing that distinguishes one session
    from another, so all eleven were byte-identical apart from their id,
    and `inbox()` reads by alias, so the recipient saw eleven copies.

    Nothing about that was a retry, and nothing about it was visible to
    the sender: the confirmation listed the same label eleven times.
    """

    def _register_many(self, alias, host, n):
        """Eleven registrations of one alias, as the registry used to
        allow.

        The unique index added with the ``(alias, host)`` rule means the
        *store* can no longer be talked into this state — see
        :class:`OneRegistrationPerAliasHostTests`. That makes storage
        the first line of defence and leaves this class testing the
        second: ``send()`` must still collapse repeated destinations,
        because resolution — broadcast especially — can hand it the same
        mailbox more than once without any row being duplicated. A guard
        that is only correct while its input is well-formed is the guard
        that failed here the first time.
        """
        fanout = getattr(self, "_fanout", None)
        if fanout is None:
            fanout = self._fanout = []
            self.store.sessions = lambda **kw: list(self._fanout)
        fanout.extend(sess(alias, host, sid=f"{alias}-{host}-{i}")
                      for i in range(n))

    def test_eleven_registrations_deliver_once(self):
        self._register_many("xollama", self.HOST, 11)
        self.assertEqual(len(self.store.sessions()), 11)
        res = self.store.send(f"xollama@{self.HOST}", "s", "b",
                              from_alias="me")
        self.assertEqual(len(res["ids"]), 1,
                         "one send, one row per mailbox")
        self.assertIsNone(res["broadcast_group"],
                          "a single mailbox is not a broadcast")
        self.assertEqual(res["destinations"], [("xollama", self.HOST)])

    def test_the_recipient_sees_one_copy(self):
        self._register_many("xollama", self.HOST, 11)
        self.store.send(f"xollama@{self.HOST}", "s", "b", from_alias="me")
        self.assertEqual(len(self.store.inbox(alias="xollama")), 1)

    def test_a_restarted_session_does_not_make_its_alias_unaddressable(self):
        """The bare-alias guard counted rows where it meant hosts.

        With eleven registrations on one host it raised "`xollama` is
        registered on 1 hosts: solidpc" and refused to send — a session
        that merely restarted eleven times locked its own alias out.
        """
        self._register_many("xollama", self.HOST, 11)
        res = self.store.send("xollama", "s", "b", from_alias="me")
        self.assertEqual(len(res["ids"]), 1)

    def test_a_genuine_cross_host_ambiguity_still_refuses(self):
        # The guard must keep working: same alias, two hosts, two
        # mailboxes, and no way to pick one.
        self.register("osync", "solidpc")
        self.register("osync", "pandorum")
        with self.assertRaises(AddressError):
            self.store.send("osync", "s", "b", from_alias="me")

    def test_broadcast_counts_hosts_not_registrations(self):
        self._register_many("osync", "solidpc", 4)
        self._register_many("osync", "pandorum", 3)
        res = self.store.send("osync*", "s", "b", from_alias="me")
        self.assertEqual(len(res["ids"]), 2, "one row per host, not per row")
        self.assertIsNotNone(res["broadcast_group"])
        self.assertEqual(sorted(h for _, h in res["destinations"]),
                         ["pandorum", "solidpc"])

    def test_the_confirmation_names_each_mailbox_once(self):
        from claude_hooks.mailbox.addressing import (
            describe_recipients,
            parse_address,
        )
        self._register_many("xollama", self.HOST, 11)
        sessions = self.store.sessions()
        addr = parse_address(f"xollama@{self.HOST}")
        text = describe_recipients(sessions, addr)
        self.assertEqual(text.count("xollama"), 1,
                         f"repeated the same mailbox: {text}")


class ForgetTests(StoreHarness):
    """SessionEnd drops the registration, or dead rows pile up.

    Nothing unregistered a session, and a row lived until the 30-day
    sweep, so an alias listed every session that had *ever* run in the
    directory. Ten short sessions in ten minutes is all it took.
    """

    def test_forget_removes_only_that_session(self):
        # Two aliases rather than two registrations of one: since the
        # ``(alias, host)`` rule, the second would have evicted the
        # first and this would pass without forget() doing anything.
        self.register("osync", self.HOST, sid="live")
        self.register("xollama", self.HOST, sid="dead")
        self.assertTrue(self.store.forget("dead"))
        left = [s.session_id for s in self.store.sessions()]
        self.assertEqual(left, ["live"])

    def test_forgetting_an_unknown_session_is_not_an_error(self):
        self.assertFalse(self.store.forget("never-existed"))

    def test_session_end_unregisters(self):
        # The wiring, not the store: a fix nothing calls changes nothing.
        import inspect
        from claude_hooks.hooks import session_end
        src = inspect.getsource(session_end)
        self.assertIn("_unregister_mailbox_session", src)
        self.assertIn("unregister_session", src)


class StaleEvictionTests(StoreHarness):
    """A registration is evidence a session *was* running.

    Ten sessions that ran and ended inside ten minutes left ten rows
    beside the live one, and the only eviction was a 30-day sweep — the
    right horizon for archiving mail and the wrong one for addressing.
    Three layers now: stale rows are not addressees, they are physically
    evicted on hours, and a quiet session re-creates its own row so
    eviction can never silence it.
    """

    def _age(self, session_id, hours):
        """Backdate last_seen, the way a session that ended looks."""
        from datetime import timedelta
        from claude_hooks.mailbox.store import utcnow
        cutoff = self.store._at(utcnow() - timedelta(hours=hours))
        with self.db.lock:
            conn = self.db()
            cur = conn.cursor()
            cur.execute("UPDATE session_registry SET last_seen = ? "
                        "WHERE session_id = ?", (cutoff, session_id))
            conn.commit()

    def test_a_stale_row_is_not_an_addressee(self):
        # One alias, two hosts — the only way one alias can now hold a
        # live row and a dead one at the same time.
        self.register("osync", self.HOST, sid="live")
        self.register("osync", "elsewhere", sid="ended")
        self._age("ended", 48)
        live = [s.session_id for s in self.store.sessions()]
        self.assertEqual(live, ["live"])

    def test_an_operator_can_still_see_stale_rows(self):
        self.register("osync", self.HOST, sid="ended")
        self._age("ended", 48)
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(
            [s.session_id for s in self.store.sessions(include_stale=True)],
            ["ended"])

    def test_a_stale_row_does_not_multiply_delivery(self):
        # The reported bug, from the other direction: even before the
        # physical eviction runs, a dead row cannot take a copy.
        #
        # Ten dead rows beside the live one cannot be *registered* any
        # more, so they are aged across hosts instead. Rebuilding this
        # with repeated registrations on one host would leave a single
        # row and pass without exercising anything.
        self.register("xollama", self.HOST, sid="live")
        for i in range(10):
            self.register("xollama", f"ended-host-{i}", sid=f"ended-{i}")
            self._age(f"ended-{i}", 48)
        res = self.store.send("xollama*", "s", "b", from_alias="me")
        self.assertEqual(len(res["ids"]), 1)
        self.assertEqual(res["destinations"], [("xollama", self.HOST)])

    def test_evict_stale_removes_only_the_stale(self):
        self.register("osync", self.HOST, sid="live")
        self.register("osync", "elsewhere", sid="ended")
        self._age("ended", 48)
        self.assertEqual(self.store.evict_stale(hours=24), 1)
        self.assertEqual(
            [s.session_id for s in self.store.sessions(include_stale=True)],
            ["live"])

    def test_eviction_is_later_than_the_live_window(self):
        # A row must stop being *used* before it stops being *readable*:
        # those ten dead rows were the only evidence of what happened.
        from claude_hooks.mailbox.store import (
            DEFAULT_EVICT_HOURS,
            DEFAULT_LIVE_HOURS,
        )
        self.assertGreater(DEFAULT_EVICT_HOURS, DEFAULT_LIVE_HOURS)

    def test_touch_recreates_an_evicted_live_session(self):
        """Eviction must not silence a session that is merely quiet.

        ``touch`` runs once per turn, so an open session with an idle
        user falls outside the window on its own. Its next turn has to
        put it back.
        """
        self.register("osync", self.HOST, sid="quiet")
        self.assertEqual(self.store.evict_stale(hours=0), 1)
        self.assertEqual(self.store.sessions(include_stale=True), [])

        self.store.touch("quiet", alias="osync", host=self.HOST)
        back = self.store.sessions()
        self.assertEqual([s.session_id for s in back], ["quiet"])
        self.assertEqual(back[0].alias, "osync")

    def test_touch_without_an_alias_cannot_rebuild(self):
        # Nothing to rebuild from, so it stays best-effort as before
        # rather than inventing a registration.
        self.store.touch("unknown-session")
        self.assertEqual(self.store.sessions(include_stale=True), [])

    def test_the_maintenance_sweep_evicts(self):
        import inspect
        from claude_hooks.mailbox import archive
        src = inspect.getsource(archive.sweep)
        self.assertIn("evict_stale", src)


class DedupeMessagesTests(StoreHarness):
    """The repair for mailboxes that already ran the fan-out.

    Fixing `send()` stops new duplicates; it does nothing about the rows
    already written. On solidpc that was 30 redundant rows of 65 — three
    sends of eleven copies each — and leaving them makes the recipient
    re-read the same message eleven times after the upgrade.

    This was first done as one-off SQL against the live table, which is
    exactly the kind of change nothing covers. Hence these.
    """

    def _register_many(self, alias, host, n):
        for i in range(n):
            self.register(alias, host, sid=f"{alias}-{host}-{i}")

    def _fan_out(self, n=11, *, alias="xollama", subject="s", body="b"):
        """Write the duplicate rows the old send() produced.

        One INSERT per registration, identical but for the id — what the
        code did before, reproduced directly so the repair is tested
        against the shape it exists for rather than a guess at it.
        """
        from claude_hooks.mailbox.store import utcnow
        created = self.store._at(utcnow())
        expires = self.store._at(utcnow())
        group = "grp-1"
        ids = []
        with self.db.lock:
            conn = self.db()
            cur = conn.cursor()
            for _ in range(n):
                cur.execute(
                    "INSERT INTO session_messages "
                    "(created_at, from_alias, from_host, to_alias, to_host, "
                    " broadcast_group, subject, body, priority, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (created, "opencoti", self.HOST, alias, self.HOST,
                     group, subject, body, expires))
                ids.append(cur.lastrowid)
            conn.commit()
        return ids

    def _all_rows(self, alias):
        """Every row for ``alias`` on any host.

        ``inbox()`` is deliberately host-scoped — mail for
        ``alias@pandorum`` is not solidpc's to read — so a test about
        what *exists* cannot go through it.
        """
        with self.db.lock:
            conn = self.db()
            cur = conn.cursor()
            cur.execute("SELECT id, to_host, broadcast_group, read_at "
                        "FROM session_messages WHERE to_alias = ? "
                        "ORDER BY id", (alias,))
            return [dict(zip(("id", "to_host", "broadcast_group", "read_at"),
                             r)) for r in cur.fetchall()]

    def _mark_read(self, message_id):
        with self.db.lock:
            conn = self.db()
            cur = conn.cursor()
            cur.execute("UPDATE session_messages SET read_at = ?, "
                        "read_by = ? WHERE id = ?",
                        (self.store._now(), "reader", message_id))
            conn.commit()

    def test_eleven_copies_collapse_to_one(self):
        self._fan_out(11)
        self.assertEqual(len(self.store.inbox(alias="xollama")), 11)
        res = self.store.dedupe_messages()
        self.assertEqual(res["removed"], 10)
        self.assertEqual(len(self.store.inbox(alias="xollama")), 1)

    def test_the_read_copy_is_the_one_kept(self):
        """The trap that made this worth doing carefully.

        Read state lives on the row and the copies do not share it. One
        live set had a single read copy in eleven, so keeping the lowest
        id had a ~91% chance of resurfacing a message already read.
        """
        ids = self._fan_out(11)
        self._mark_read(ids[7])          # deliberately not the lowest id
        self.store.dedupe_messages()
        left = self.store.inbox(alias="xollama", include_read=True)
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["id"], ids[7])
        self.assertIsNotNone(left[0]["read_at"])
        # ...and it does not come back as unread.
        self.assertEqual(self.store.inbox(alias="xollama"), [])

    def test_a_collapsed_group_is_no_longer_a_broadcast(self):
        self._fan_out(11)
        self.store.dedupe_messages()
        left = self.store.inbox(alias="xollama", include_read=True)
        self.assertIsNone(left[0]["broadcast_group"],
                          "one destination is not a broadcast")

    def test_a_genuine_two_host_broadcast_is_untouched(self):
        # Different hosts mean different destinations, so those rows are
        # not duplicates of each other and the group is real.
        self.register("osync", "solidpc")
        self.register("osync", "pandorum")
        res = self.store.send("osync*", "s", "b", from_alias="me")
        self.assertEqual(len(res["ids"]), 2)
        self.assertEqual(self.store.dedupe_messages()["removed"], 0)
        rows = self._all_rows("osync")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["broadcast_group"] for r in rows),
                        "cleared a group that still has two destinations")

    def test_a_broadcast_that_also_fanned_out_keeps_its_group(self):
        """Both kinds of multiplicity at once — only one is spurious."""
        from claude_hooks.mailbox.store import utcnow
        created = self.store._at(utcnow())
        with self.db.lock:
            conn = self.db()
            cur = conn.cursor()
            for host, n in (("solidpc", 4), ("pandorum", 3)):
                for _ in range(n):
                    cur.execute(
                        "INSERT INTO session_messages "
                        "(created_at, from_alias, from_host, to_alias, "
                        " to_host, broadcast_group, subject, body, priority, "
                        " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                        (created, "me", self.HOST, "osync", host, "grp-2",
                         "s", "b", created))
            conn.commit()
        res = self.store.dedupe_messages()
        self.assertEqual(res["removed"], 5, "4+3 rows are 2 destinations")
        rows = self._all_rows("osync")
        self.assertEqual(sorted(r["to_host"] for r in rows),
                         ["pandorum", "solidpc"])
        self.assertTrue(all(r["broadcast_group"] == "grp-2" for r in rows),
                        "a two-host broadcast is still a broadcast")

    def test_distinct_messages_are_never_merged(self):
        self.register("osync", self.HOST)
        self.store.send("osync", "first", "body", from_alias="me")
        self.store.send("osync", "second", "body", from_alias="me")
        self.store.send("osync", "first", "different", from_alias="me")
        self.assertEqual(self.store.dedupe_messages()["removed"], 0)
        self.assertEqual(len(self.store.inbox(alias="osync")), 3)

    def test_two_deliberate_sends_of_identical_text_both_survive(self):
        """``dedupe_messages`` must still not collapse two genuine sends.

        It keys on ``created_at`` to the microsecond precisely so that it
        only ever removes rows from one ``INSERT`` loop. That property is
        unchanged — but reaching it now takes a first message that is read
        and older than the dedup window, because ``send()`` refuses an
        immediate identical repeat (see
        :class:`RefuseIdenticalResendTests`). The behaviour this test
        asserted before that guard — a second identical send going
        straight through — is deliberately gone.
        """
        self.register("osync", self.HOST)
        first = self.store.send("osync", "same", "same",
                                from_alias="me")["ids"][0]
        self.store.read([first], reader_session="osync-solidpc",
                        alias="osync", host=self.HOST)
        self._age_message(first, seconds=3600)

        self.store.send("osync", "same", "same", from_alias="me")

        self.assertEqual(self.store.dedupe_messages()["removed"], 0)
        self.assertEqual(
            len(self.store.inbox(alias="osync", include_read=True)), 2)

    def _age_message(self, message_id, *, seconds):
        old = utcnow() - timedelta(seconds=seconds)
        with self.db.lock:
            conn = self.db()
            conn.execute("UPDATE session_messages SET created_at = ?"
                         " WHERE id = ?", (old.isoformat(), message_id))
            conn.commit()

    def test_it_is_idempotent(self):
        self._fan_out(11)
        self.assertEqual(self.store.dedupe_messages()["removed"], 10)
        self.assertEqual(self.store.dedupe_messages()["removed"], 0)
        self.assertEqual(self.store.dedupe_messages()["groups_cleared"], 0)

    def test_an_empty_mailbox_is_not_an_error(self):
        self.assertEqual(self.store.dedupe_messages(),
                         {"removed": 0, "kept": 0, "groups_cleared": 0})

    def test_the_maintenance_sweep_repairs(self):
        # A repair nothing calls repairs nothing: every host that ran the
        # old code has these rows, and only the sweep reaches them.
        import inspect
        from claude_hooks.mailbox import archive
        src = inspect.getsource(archive.sweep)
        self.assertIn("dedupe_messages", src)

    def test_it_spans_more_than_one_delete_chunk(self):
        # The delete is chunked at 500 ids; a mailbox that ran the bug
        # for a while has more than that.
        self._fan_out(600)
        self.assertEqual(self.store.dedupe_messages()["removed"], 599)
        self.assertEqual(len(self.store.inbox(alias="xollama")), 1)


if __name__ == "__main__":
    unittest.main()


class AnnounceTests(unittest.TestCase):
    """What the hooks inject. The rule that matters is what is absent."""

    def msg(self, **kw):
        base = {"id": 1, "subject": "LSP MCP wedges on multi-byte hover",
                "from_alias": "opencoti", "from_host": "solidpc",
                "created_at": utcnow().isoformat(), "priority": 0,
                "body": "SECRET BODY THAT MUST NOT APPEAR"}
        base.update(kw)
        return base

    def test_body_is_never_injected(self):
        """Not for high priority, not for a short one. A body in the
        prompt is an interruption whether or not it was urgent."""
        from claude_hooks.mailbox import announce
        for pri in (0, 1, 3):
            with self.subTest(priority=pri):
                out = announce.render([self.msg(priority=pri)], [],
                                      alias="me", host="h")
                self.assertNotIn("SECRET BODY", out)

    def test_carries_exactly_subject_sender_time_priority(self):
        from claude_hooks.mailbox import announce
        out = announce.render([self.msg(priority=2)], [], alias="me", host="h")
        self.assertIn("LSP MCP wedges", out)
        self.assertIn("opencoti@solidpc", out)
        self.assertIn("just now", out)
        self.assertIn("[high]", out)

    def test_nothing_to_say_renders_nothing(self):
        """A heading that appears every turn saying 'no messages' trains
        the reader to skip the heading."""
        from claude_hooks.mailbox import announce
        self.assertEqual(announce.render([], [], alias="me", host="h"), "")

    def test_receipt_note_is_shown_inline(self):
        """Consistent, not an exception: an ack is short by construction
        and already delivered in full, so fetching would cost more."""
        from claude_hooks.mailbox import announce
        out = announce.render([], [{
            "id": 4, "subject": "the ask", "read_by": "osync",
            "read_at": utcnow().isoformat(),
            "ack_body": "confirmed, ~2h"}], alias="me", host="h")
        self.assertIn("1 receipt", out)
        self.assertIn("confirmed, ~2h", out)
        self.assertIn("osync", out)

    def test_ago_is_relative(self):
        from claude_hooks.mailbox.announce import ago
        from datetime import timedelta
        now = utcnow()
        self.assertEqual(ago(now.isoformat(), now=now), "just now")
        self.assertEqual(ago((now - timedelta(minutes=4)).isoformat(),
                             now=now), "4 min ago")
        self.assertEqual(ago((now - timedelta(hours=2)).isoformat(),
                             now=now), "2 hours ago")
        self.assertEqual(ago((now - timedelta(days=3)).isoformat(),
                             now=now), "3 days ago")

    def test_ago_survives_garbage(self):
        from claude_hooks.mailbox.announce import ago
        self.assertEqual(ago(None), "unknown time")
        self.assertEqual(ago("not a date"), "unknown time")

    def test_collision_note_names_the_forms(self):
        from claude_hooks.mailbox.announce import collision_note
        note = collision_note([sess("osync", "pandorum")], "osync")
        self.assertIn("pandorum", note)
        self.assertIn("osync@pandorum", note)
        self.assertIn("osync*", note)

    def test_no_collision_no_note(self):
        from claude_hooks.mailbox.announce import collision_note
        self.assertEqual(collision_note([], "osync"), "")


class ToolsTests(StoreHarness):

    def setUp(self):
        super().setUp()
        from claude_hooks.mailbox.tools import MailboxTools
        self.register("osync", "solidpc", sid="s-osync")
        self.register("claude-hooks", "solidpc", sid="s-ch")
        self.me = MailboxTools(self.store, alias="claude-hooks",
                               session_id="s-ch", host="solidpc")
        self.them = MailboxTools(self.store, alias="osync",
                                 session_id="s-osync", host="solidpc")

    def test_catalog_is_eight_tools(self):
        from claude_hooks.mailbox.tools import TOOL_NAMES, tool_catalog
        self.assertEqual(len(tool_catalog()), 8)
        self.assertEqual({t["name"] for t in tool_catalog()}, set(TOOL_NAMES))

    def test_every_tool_declares_a_schema(self):
        from claude_hooks.mailbox.tools import tool_catalog
        for t in tool_catalog():
            with self.subTest(tool=t["name"]):
                self.assertTrue(t["description"].strip())
                self.assertEqual(t["inputSchema"]["type"], "object")

    def test_send_then_list_then_read(self):
        self.me.call("mailbox-send", {"to": "osync", "subject": "s",
                                      "body": "the body"})
        listed = self.them.call("mailbox-list", {})
        self.assertIn("unread", listed)
        self.assertNotIn("the body", listed, "list must not carry bodies")
        read = self.them.call("mailbox-read", {"ids": [1]})
        self.assertIn("the body", read)

    def test_ambiguous_send_returns_prose_not_an_exception(self):
        self.register("osync", "pandorum", sid="p-osync")
        out = self.me.call("mailbox-send", {"to": "osync", "subject": "s",
                                            "body": "b"})
        self.assertIn("Nothing was sent", out)
        self.assertIn("osync@solidpc", out)

    def test_reading_someone_elses_id_says_so(self):
        self.me.call("mailbox-send", {"to": "osync", "subject": "s",
                                      "body": "b"})
        out = self.me.call("mailbox-read", {"ids": [1]})
        self.assertIn("addressed to you", out)

    def test_sent_shows_read_state_and_note(self):
        self.me.call("mailbox-send", {"to": "osync", "subject": "s",
                                      "body": "b"})
        self.them.call("mailbox-read", {"ids": [1]})
        self.them.call("mailbox-ack", {"id": 1, "note": "on it"})
        out = self.me.call("mailbox-sent", {})
        self.assertIn("read by s-osync", out)
        self.assertIn("on it", out)

    def test_sent_marks_the_receipt_seen_and_freezes_the_ack(self):
        self.me.call("mailbox-send", {"to": "osync", "subject": "s",
                                      "body": "b"})
        self.them.call("mailbox-read", {"ids": [1]})
        self.them.call("mailbox-ack", {"id": 1, "note": "first"})
        self.me.call("mailbox-sent", {})
        out = self.them.call("mailbox-ack", {"id": 1, "note": "second"})
        self.assertIn("frozen", out)

    def test_edit_then_refused_after_read(self):
        self.me.call("mailbox-send", {"to": "osync", "subject": "v1",
                                      "body": "b"})
        self.assertIn("Updated",
                      self.me.call("mailbox-edit", {"id": 1,
                                                    "subject": "v2"}))
        self.them.call("mailbox-read", {"ids": [1]})
        self.assertIn("no longer be edited",
                      self.me.call("mailbox-edit", {"id": 1,
                                                    "subject": "v3"}))

    def test_cancel(self):
        self.me.call("mailbox-send", {"to": "osync", "subject": "s",
                                      "body": "b"})
        self.assertIn("Withdrawn", self.me.call("mailbox-cancel", {"id": 1}))
        self.assertIn("No messages", self.them.call("mailbox-list", {}))

    def test_sessions_warns_about_a_spanning_alias(self):
        self.register("osync", "pandorum", sid="p-osync")
        out = self.me.call("mailbox-sessions", {})
        self.assertIn("spans 2 hosts", out)

    def test_sessions_filters_by_os(self):
        """Pinned relative to the running platform.

        Asserting that "windows" finds nothing passes on Linux and fails
        on Windows, where these sessions genuinely are Windows sessions —
        which is how it was written, and why pandorum caught it.
        """
        from claude_hooks.mailbox.store import os_name
        here = os_name()
        elsewhere = "darwin" if here != "darwin" else "linux"
        self.assertIn("session(s)", self.me.call("mailbox-sessions",
                                                 {"os": here}))
        self.assertIn("No sessions", self.me.call("mailbox-sessions",
                                                  {"os": elsewhere}))

    def test_read_with_no_ids(self):
        self.assertIn("Pass ids", self.them.call("mailbox-read", {}))

    def test_handles_reports_ownership(self):
        self.assertTrue(self.me.handles("mailbox-send"))
        self.assertFalse(self.me.handles("pgvector-find"))


class HookIntegrationTests(StoreHarness):
    """The hooks' job is to announce without costing anything."""

    def setUp(self):
        super().setUp()
        from claude_hooks.mailbox import hook as hookmod
        self.hookmod = hookmod
        self.cfg = {"hooks": {"mailbox": {"enabled": True}}}

        store = self.store

        class FakeProvider:
            name = "pgvector"

        from claude_hooks.mailbox.tools import MailboxTools
        self.tools = MailboxTools(store, alias="me", session_id="mine",
                                  host="solidpc")
        hookmod._tools = lambda config, providers, event=None: (
            self.tools if providers else None)
        self.addCleanup(self._restore, hookmod)
        self._orig = hookmod._tools
        self.provider = FakeProvider()
        self.register("me", "solidpc", sid="mine")

    def _restore(self, hookmod):
        import importlib
        importlib.reload(hookmod)

    def test_disabled_by_default(self):
        self.assertFalse(self.hookmod._enabled({}))
        self.assertEqual(self.hookmod.announce_block(
            event={}, config={}, providers=[self.provider]), "")

    def test_announces_unread_mail(self):
        self.store.send("me", "the subject", "the body", from_alias="them")
        out = self.hookmod.announce_block(
            event={"session_id": "mine"}, config=self.cfg,
            providers=[self.provider])
        self.assertIn("the subject", out)
        self.assertNotIn("the body", out, "hooks never inject a body")

    def test_silent_when_there_is_nothing(self):
        self.assertEqual(self.hookmod.announce_block(
            event={"session_id": "mine"}, config=self.cfg,
            providers=[self.provider]), "")

    def test_a_broken_mailbox_costs_nothing(self):
        """No announcement beats a delayed prompt — the model can always
        call mailbox-list itself."""
        def boom(*a, **k):
            raise RuntimeError("db on fire")
        self.tools.store.inbox = boom          # type: ignore
        self.assertEqual(self.hookmod.announce_block(
            event={"session_id": "mine"}, config=self.cfg,
            providers=[self.provider]), "")

    def test_no_provider_is_not_an_error(self):
        self.assertEqual(self.hookmod.announce_block(
            event={}, config=self.cfg, providers=[]), "")

    def test_since_limits_to_mid_turn_arrivals(self):
        """What stops the Stop hook repeating UserPromptSubmit."""
        import time
        self.store.send("me", "before", "b", from_alias="them")
        cut = utcnow()
        time.sleep(0.01)
        self.store.send("me", "after", "b", from_alias="them")
        out = self.hookmod.announce_block(
            event={"session_id": "mine"}, config=self.cfg,
            providers=[self.provider], since=cut)
        self.assertIn("after", out)
        self.assertNotIn("before", out)

    def test_receipts_are_announced_then_marked_seen(self):
        self.store.send("other", "s", "b", from_alias="me")
        mid = self.store.sent(from_alias="me")[0]["id"]
        self.register("other", "solidpc", sid="o1")
        self.store.read([mid], reader_session="o1", alias="other",
                        host="solidpc")
        self.store.ack(mid, "on it", session_id="o1", alias="other",
                       host="solidpc")

        first = self.hookmod.announce_block(
            event={"session_id": "mine"}, config=self.cfg,
            providers=[self.provider])
        self.assertIn("on it", first)
        second = self.hookmod.announce_block(
            event={"session_id": "mine"}, config=self.cfg,
            providers=[self.provider])
        self.assertNotIn("on it", second,
                         "a receipt must not repeat forever")

    def test_turn_start_parses_or_returns_none(self):
        self.assertIsNone(self.hookmod.turn_start({}))
        self.assertIsNone(self.hookmod.turn_start({"started_at": "nonsense"}))
        got = self.hookmod.turn_start({"started_at": utcnow().isoformat()})
        self.assertIsNotNone(got)


class ArchiveTests(StoreHarness):
    """Nothing is deleted before its durable copy is on disk."""

    def setUp(self):
        super().setUp()
        from claude_hooks.mailbox import archive
        self.archive = archive
        self.dir = Path(self._tmp.name) / "arch"
        # `self.archive` is the module, so anything stubbed on it leaks
        # into every later test in this class.
        self._orig_write = archive.write
        self.addCleanup(setattr, archive, "write", self._orig_write)

    def expire_one(self, subject="s"):
        self.store.send("x", subject, "body", from_alias="me",
                        expires_days=-1)

    def test_write_then_read_back(self):
        self.expire_one()
        rows = self.store.expired()
        files = self.archive.write(rows, directory=self.dir)
        self.assertEqual(len(files), 1)
        back = self.archive._decode(files[0].read_bytes())
        self.assertEqual(back[0]["subject"], "s")

    def test_written_file_is_compressed(self):
        self.expire_one()
        files = self.archive.write(self.store.expired(), directory=self.dir)
        self.assertEqual(files[0].read_bytes()[:4], b"\x28\xb5\x2f\xfd")

    def test_append_keeps_earlier_rows(self):
        self.expire_one("first")
        self.archive.write(self.store.expired(), directory=self.dir)
        ids = [r["id"] for r in self.store.expired()]
        self.store.delete(ids)
        self.expire_one("second")
        files = self.archive.write(self.store.expired(), directory=self.dir)
        subjects = {r["subject"]
                    for r in self.archive._decode(files[0].read_bytes())}
        self.assertEqual(subjects, {"first", "second"})

    def test_sweep_archives_before_deleting(self):
        self.expire_one()
        res = self.archive.sweep(self.store, directory=self.dir)
        self.assertEqual(res["expired"], 1)
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(self.store.expired(), [])
        self.assertTrue(list(self.dir.glob("*.jsonl.zst")))

    def test_sweep_does_not_delete_when_the_archive_fails(self):
        """The failure mode of the other order is silent data loss that
        looks like successful housekeeping."""
        self.expire_one()
        self.archive.write = lambda *a, **k: []     # type: ignore
        res = self.archive.sweep(self.store, directory=self.dir)
        self.assertEqual(res["deleted"], 0)
        self.assertEqual(len(self.store.expired()), 1,
                         "rows must survive a failed archive")

    def test_fresh_messages_are_untouched(self):
        self.store.send("x", "keep", "b", from_alias="me")
        res = self.archive.sweep(self.store, directory=self.dir)
        self.assertEqual(res["expired"], 0)
        self.assertEqual(len(self.store.sent(from_alias="me")), 1)

    def test_cap_drops_oldest_quarter_first(self):
        self.dir.mkdir(parents=True)
        for name in ("2025-Q1", "2025-Q2", "2026-Q1"):
            (self.dir / f"{name}.jsonl.zst").write_bytes(b"x" * 1000)
        dropped = self.archive.enforce_cap(directory=self.dir,
                                           cap_bytes=1500)
        self.assertEqual(dropped, ["2025-Q1.jsonl.zst", "2025-Q2.jsonl.zst"])
        self.assertTrue((self.dir / "2026-Q1.jsonl.zst").is_file())

    def test_cap_does_nothing_under_the_limit(self):
        self.dir.mkdir(parents=True)
        (self.dir / "2026-Q1.jsonl.zst").write_bytes(b"x" * 10)
        self.assertEqual(self.archive.enforce_cap(directory=self.dir,
                                                  cap_bytes=1000), [])

    def test_cap_on_a_missing_directory(self):
        self.assertEqual(self.archive.enforce_cap(
            directory=self.dir / "nope"), [])

    def test_corrupt_archive_is_set_aside_not_overwritten(self):
        self.dir.mkdir(parents=True)
        bad = self.dir / f"{self.archive.quarter_of(None)}.jsonl.zst"
        bad.write_bytes(b"\x28\xb5\x2f\xfdnot really zstd")
        self.expire_one()
        self.archive.write(self.store.expired(), directory=self.dir)
        self.assertTrue(list(self.dir.glob("*.corrupt")),
                        "the unreadable file must be kept")
        self.assertTrue(bad.is_file())

    def test_sweep_also_forgets_stale_sessions(self):
        self.register("old", "h1")
        cutoff = utcnow() - timedelta(days=40)
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE session_registry SET last_seen = ?",
                (cutoff.isoformat(),))
            self.db.conn.commit()
        res = self.archive.sweep(self.store, directory=self.dir,
                                 registry_days=30)
        self.assertEqual(res["sessions_forgotten"], 1)

    def test_quarter_boundaries(self):
        for month, q in ((1, "Q1"), (3, "Q1"), (4, "Q2"), (6, "Q2"),
                         (7, "Q3"), (9, "Q3"), (10, "Q4"), (12, "Q4")):
            with self.subTest(month=month):
                ts = f"2026-{month:02d}-15T00:00:00+00:00"
                self.assertEqual(self.archive.quarter_of(ts), f"2026-{q}")

    def test_cap_default_is_ten_gb(self):
        self.assertEqual(self.archive.DEFAULT_CAP_BYTES,
                         10 * 1024 * 1024 * 1024)


class SharedAliasAcrossHostsTests(StoreHarness):
    """The alias is not an identity — only alias@host is.

    The default alias is the project directory name, so two hosts with
    the same repo checked out are both ``claude-hooks``. The addressing
    layer already knows this: it is exactly the case where a bare alias
    is refused. The sender-side queries did not, and scoped on the alias
    alone, which handed each host authority over the other's mail.

    Found live on 2026-09-16: pandorum's ``mailbox-sent`` listed a
    message solidpc had sent.
    """

    ALIAS = "claude-hooks"

    def setUp(self):
        super().setUp()
        self.register(self.ALIAS, "solidpc", sid="sol-1")
        self.register(self.ALIAS, "pandorum", sid="pan-1")

    def send_from(self, host, to, subject="s", body="b"):
        """Send with ``host_name()`` pinned, as the real host would."""
        from unittest import mock
        from claude_hooks.mailbox import store as store_mod
        with mock.patch.object(store_mod, "host_name", lambda: host):
            return self.store.send(to, subject, body, from_alias=self.ALIAS,
                                   from_session=f"{host}-sid")

    # ─── the outbox ──────────────────────────────────────────────────

    def test_sent_shows_only_this_hosts_messages(self):
        self.send_from("solidpc", f"{self.ALIAS}@pandorum", subject="from-sol")
        self.send_from("pandorum", f"{self.ALIAS}@solidpc", subject="from-pan")

        sol = self.store.sent(from_alias=self.ALIAS, from_host="solidpc")
        pan = self.store.sent(from_alias=self.ALIAS, from_host="pandorum")
        self.assertEqual([m["subject"] for m in sol], ["from-sol"])
        self.assertEqual([m["subject"] for m in pan], ["from-pan"])

    # ─── the receipt, which is the one that lost data ────────────────

    def test_one_host_cannot_consume_the_others_pending_receipt(self):
        res = self.send_from("solidpc", f"{self.ALIAS}@pandorum")
        mid = res["ids"][0]
        self.store.read([mid], reader_session="pan-1", alias=self.ALIAS,
                        host="pandorum")
        self.store.ack(mid, "noted", session_id="pan-1", alias=self.ALIAS,
                       host="pandorum")

        # pandorum checks its own outbox. Before the fix this marked
        # solidpc's receipt seen, so solidpc was never told.
        self.store.mark_receipts_seen(
            [mid], from_alias=self.ALIAS, from_host="pandorum")

        still = self.store.pending_receipts(from_alias=self.ALIAS,
                                            from_host="solidpc")
        self.assertEqual([r["id"] for r in still], [mid],
                         "pandorum swallowed solidpc's receipt")

    def test_the_sender_can_still_see_its_own_receipt(self):
        res = self.send_from("solidpc", f"{self.ALIAS}@pandorum")
        mid = res["ids"][0]
        self.store.read([mid], reader_session="pan-1", alias=self.ALIAS,
                        host="pandorum")
        self.store.ack(mid, "noted", session_id="pan-1", alias=self.ALIAS,
                       host="pandorum")

        self.assertEqual(
            [r["id"] for r in self.store.pending_receipts(
                from_alias=self.ALIAS, from_host="solidpc")], [mid])
        self.assertEqual(self.store.mark_receipts_seen(
            [mid], from_alias=self.ALIAS, from_host="solidpc"), 1)
        self.assertEqual(self.store.pending_receipts(
            from_alias=self.ALIAS, from_host="solidpc"), [])

    # ─── authority over the body ─────────────────────────────────────

    def test_one_host_cannot_edit_the_others_message(self):
        mid = self.send_from("solidpc", f"{self.ALIAS}@pandorum",
                             subject="original")["ids"][0]
        with self.assertRaises(MailboxError) as ctx:
            self.store.edit(mid, from_alias=self.ALIAS, from_host="pandorum",
                            subject="hijacked")
        msg = str(ctx.exception)
        self.assertIn("solidpc", msg)
        self.assertIn("pandorum", msg)
        row = self.store.sent(from_alias=self.ALIAS, from_host="solidpc")[0]
        self.assertEqual(row["subject"], "original")

    def test_one_host_cannot_cancel_the_others_message(self):
        mid = self.send_from("solidpc", f"{self.ALIAS}@pandorum")["ids"][0]
        with self.assertRaises(MailboxError):
            self.store.cancel(mid, from_alias=self.ALIAS,
                              from_host="pandorum")
        self.assertIsNone(
            self.store.sent(from_alias=self.ALIAS,
                            from_host="solidpc")[0]["cancelled_at"])

    def test_the_sender_can_still_edit_its_own_message(self):
        mid = self.send_from("solidpc", f"{self.ALIAS}@pandorum",
                             subject="original")["ids"][0]
        self.store.edit(mid, from_alias=self.ALIAS, from_host="solidpc",
                        subject="corrected")
        self.assertEqual(
            self.store.sent(from_alias=self.ALIAS,
                            from_host="solidpc")[0]["subject"], "corrected")

    def test_the_refusal_names_both_sides(self):
        """A session called claude-hooks being told the sender was
        'claude-hooks, not you' is the unhelpful version of this."""
        mid = self.send_from("solidpc", f"{self.ALIAS}@pandorum")["ids"][0]
        with self.assertRaises(MailboxError) as ctx:
            self.store.cancel(mid, from_alias=self.ALIAS,
                              from_host="pandorum")
        self.assertIn(f"{self.ALIAS}@solidpc", str(ctx.exception))
        self.assertIn(f"{self.ALIAS}@pandorum", str(ctx.exception))

    # ─── broadcasts ──────────────────────────────────────────────────

    def test_broadcast_unread_check_is_scoped_to_its_own_sender(self):
        """Two hosts broadcasting to the same alias produce two groups;
        neither may answer the other's 'is any copy still unread'."""
        self.register("worker", "h1", sid="w1")
        self.register("worker", "h2", sid="w2")
        sol = self.send_from("solidpc", "worker*")
        self.assertEqual(len(sol["ids"]), 2)
        pan = self.send_from("pandorum", "worker*")
        self.assertEqual(len(pan["ids"]), 2)

        for mid in pan["ids"]:
            self.store.read([mid], reader_session="w1", alias="worker",
                            host=self._host_of(mid))

        # solidpc's broadcast is untouched, so it stays editable.
        self.store.edit(sol["ids"][0], from_alias=self.ALIAS,
                        from_host="solidpc", subject="still-editable")
        self.assertEqual(
            self.store.sent(from_alias=self.ALIAS,
                            from_host="solidpc")[0]["subject"],
            "still-editable")

    def _host_of(self, mid):
        with self.db.lock:
            cur = self.db.conn.execute(
                "SELECT to_host FROM session_messages WHERE id = ?", (mid,))
            return cur.fetchone()[0]


class SharedAliasToolsTests(StoreHarness):
    """The tools layer must pass its own host down, not just its alias."""

    ALIAS = "claude-hooks"

    def setUp(self):
        super().setUp()
        from claude_hooks.mailbox.tools import MailboxTools
        self.register(self.ALIAS, "solidpc", sid="sol-1")
        self.register(self.ALIAS, "pandorum", sid="pan-1")
        self.sol = MailboxTools(self.store, alias=self.ALIAS,
                                session_id="sol-1", host="solidpc")
        self.pan = MailboxTools(self.store, alias=self.ALIAS,
                                session_id="pan-1", host="pandorum")

    def _send(self, tools, **kw):
        from unittest import mock
        from claude_hooks.mailbox import store as store_mod
        with mock.patch.object(store_mod, "host_name", lambda: tools.host):
            return tools.call("mailbox-send", kw)

    def test_mailbox_sent_does_not_list_the_other_hosts_mail(self):
        self._send(self.sol, to=f"{self.ALIAS}@pandorum",
                   subject="from-solidpc", body="b")
        self.assertIn("You have not sent any messages.",
                      self.pan.call("mailbox-sent", {}))
        self.assertIn("from-solidpc", self.sol.call("mailbox-sent", {}))

    def test_mailbox_edit_from_the_other_host_is_refused(self):
        out = self._send(self.sol, to=f"{self.ALIAS}@pandorum",
                         subject="original", body="b")
        mid = int(out.split("id ")[1].split(")")[0])
        refusal = self.pan.call("mailbox-edit",
                                {"id": mid, "subject": "hijacked"})
        self.assertIn("cannot rewrite", refusal)
        self.assertIn("original", self.sol.call("mailbox-sent", {}))


class OneRegistrationPerAliasHostTests(StoreHarness):
    """A restart must replace a registration, not add one.

    ``session_id`` is the primary key, so a client that came back under
    a new id left the old row behind: observed live as five
    ``xollama@solidpc`` rows in one directory, four of them an hour
    stale behind the one doing the work.
    """

    def test_a_new_session_id_replaces_the_old_row(self):
        self.register("xollama", "solidpc", sid="before-restart")
        self.register("xollama", "solidpc", sid="after-restart")

        rows = self.store.sessions(alias="xollama")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, "after-restart")

    def test_five_restarts_still_leave_one_row(self):
        for i in range(5):
            self.register("xollama", "solidpc", sid=f"run-{i}")
        rows = self.store.sessions(alias="xollama")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, "run-4")

    def test_the_same_alias_on_another_host_is_untouched(self):
        """``xollama@solidpc`` and ``xollama@pandorum`` are two
        correspondents, not one — the whole point of qualifying by
        host."""
        self.register("xollama", "solidpc", sid="sol")
        self.register("xollama", "pandorum", sid="pan")

        hosts = sorted(r.host for r in self.store.sessions(alias="xollama"))
        self.assertEqual(hosts, ["pandorum", "solidpc"])

    def test_the_database_refuses_a_duplicate(self):
        """Enforced by the index, not only by the code path that writes
        it — a second writer must not be able to recreate the state."""
        self.register("xollama", "solidpc", sid="one")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.lock:
                conn = self.db()
                conn.execute(
                    "INSERT INTO session_registry (session_id, alias, host,"
                    " os, cwd, started_at, last_seen)"
                    " VALUES ('two', 'xollama', 'solidpc', 'linux', '',"
                    " '2026-09-22T00:00:00+00:00',"
                    " '2026-09-22T00:00:00+00:00')")

    def test_collisions_reported_at_registration_are_now_cross_host_only(self):
        self.register("xollama", "solidpc", sid="sol-old")
        others = self.register("xollama", "pandorum", sid="pan")
        self.assertEqual([o.host for o in others], ["solidpc"])

        # Re-registering on solidpc reports pandorum, and does not
        # report the predecessor it just replaced.
        others = self.register("xollama", "solidpc", sid="sol-new")
        self.assertEqual([o.host for o in others], ["pandorum"])

    def test_existing_duplicates_are_cleared_when_the_schema_is_applied(self):
        """The migration. An upgrade meets a table that already has
        them, and the unique index cannot be built until they are
        gone."""
        # Relative to now: sessions() hides rows outside the live
        # window, so fixed dates stopped being "live" a day later.
        now = utcnow()
        older = (now - timedelta(minutes=10)).isoformat()
        newer = (now - timedelta(minutes=1)).isoformat()
        with self.db.lock:
            conn = self.db()
            conn.execute("DROP INDEX IF EXISTS "
                         "session_registry_alias_host_uidx")
            for sid, seen in (("a", older), ("b", newer), ("c", older)):
                conn.execute(
                    "INSERT INTO session_registry (session_id, alias, host,"
                    " os, cwd, started_at, last_seen)"
                    " VALUES (?, 'xollama', 'solidpc', 'linux', '', ?, ?)",
                    (sid, seen, seen))
            conn.commit()
        self.assertEqual(len(self.store.sessions(alias="xollama")), 3)

        self.store._ready = False          # as a fresh process would
        self.store.ensure_schema()

        rows = self.store.sessions(alias="xollama")
        self.assertEqual(len(rows), 1)
        # The most recently seen row survives — the live one.
        self.assertEqual(rows[0].session_id, "b")

    def test_touch_rebuilds_an_evicted_row_without_duplicating(self):
        """A quiet session whose row was taken over comes back on its
        next action, and comes back as one row."""
        self.register("xollama", "solidpc", sid="quiet")
        self.register("xollama", "solidpc", sid="loud")

        self.store.touch("quiet", alias="xollama", host="solidpc")

        rows = self.store.sessions(alias="xollama")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, "quiet")


class ActivityRefreshesLastSeenTests(StoreHarness):
    """Using the mailbox is the strongest evidence a session is alive,
    and it was the one signal the registry did not record."""

    def _tools(self, sid="s-live", alias="xollama"):
        from claude_hooks.mailbox.tools import MailboxTools
        return MailboxTools(self.store, alias=alias, session_id=sid,
                            host="solidpc")

    def _last_seen(self, sid):
        rows = [r for r in self.store.sessions(include_stale=True)
                if r.session_id == sid]
        self.assertEqual(len(rows), 1)
        return rows[0].last_seen

    def _age(self, sid, minutes):
        stale = utcnow() - timedelta(minutes=minutes)
        with self.db.lock:
            conn = self.db()
            conn.execute("UPDATE session_registry SET last_seen = ?"
                         " WHERE session_id = ?",
                         (stale.isoformat(), sid))
            conn.commit()

    def test_a_second_tool_call_moves_last_seen(self):
        """The first call registers. Every one after it used to leave
        the timestamp where SessionStart put it, so a session working
        inside a single long turn read as idle the whole time."""
        tools = self._tools()
        tools.call("mailbox-list", {})
        self._age("s-live", minutes=15)
        stale = self._last_seen("s-live")

        tools.call("mailbox-list", {})

        self.assertGreater(self._last_seen("s-live"), stale)

    def test_reading_and_sending_both_count_as_activity(self):
        self.register("peer", "solidpc", sid="s-peer")
        tools = self._tools()
        tools.call("mailbox-list", {})

        for name, args in (("mailbox-send", {"to": "peer", "subject": "s",
                                             "body": "b"}),
                           ("mailbox-sessions", {}),
                           ("mailbox-sent", {})):
            with self.subTest(tool=name):
                self._age("s-live", minutes=20)
                stale = self._last_seen("s-live")
                tools.call(name, args)
                self.assertGreater(self._last_seen("s-live"), stale)

    def test_activity_rebuilds_a_registration_that_was_swept(self):
        """Eviction is only safe if activity undoes it — otherwise a
        session that went quiet is unaddressable for the rest of its
        life."""
        tools = self._tools()
        tools.call("mailbox-list", {})
        self.store.forget("s-live")
        self.assertEqual(self.store.sessions(alias="xollama"), [])

        tools.call("mailbox-list", {})

        rows = self.store.sessions(alias="xollama")
        self.assertEqual([r.session_id for r in rows], ["s-live"])

    def test_a_tool_failure_does_not_cost_the_refresh(self):
        """Soft-fail runs the other way too: the refresh must not be
        skipped because the call it accompanies was rejected."""
        tools = self._tools()
        tools.call("mailbox-list", {})
        self._age("s-live", minutes=30)
        stale = self._last_seen("s-live")

        out = tools.call("mailbox-send", {"to": "nobody-here",
                                          "subject": "s", "body": "b"})

        self.assertGreater(self._last_seen("s-live"), stale)
        self.assertTrue(out)


class AliasBelongsToTheSessionTests(StoreHarness):
    """Directory is a property of a session, not the key to it."""

    def test_a_registered_session_keeps_its_name_after_a_cd(self):
        self.register("xollama", "solidpc", sid="s1")
        self.assertEqual(self.store.registered_alias("s1"), "xollama")

    def test_an_unregistered_session_has_no_pinned_name(self):
        self.assertIsNone(self.store.registered_alias("never-seen"))
        self.assertIsNone(self.store.registered_alias(""))

    def test_the_binding_prefers_the_registration_over_the_directory(self):
        """``alias_for(cwd)`` is the *default* for a session with no
        registration; it must not rename one that has."""
        from claude_hooks.mailbox import integration

        self.register("xollama", "solidpc", sid="s1")
        captured = {}

        class _Tools:
            def __init__(self, store, *, alias, session_id, host):
                captured["alias"] = alias

        import claude_hooks.mailbox.tools as tools_mod
        real_tools, real_store = tools_mod.MailboxTools, None
        tools_mod.MailboxTools = _Tools
        real_store = integration.store_for_provider
        integration.store_for_provider = lambda provider: self.store
        try:
            integration.tools_for_provider(object(), cwd="/somewhere/else",
                                           session_id="s1")
        finally:
            tools_mod.MailboxTools = real_tools
            integration.store_for_provider = real_store

        self.assertEqual(captured["alias"], "xollama")


class RefreshWithoutASessionIdTests(StoreHarness):
    """The tool path has no session id to key on.

    Claude Code does not export ``CLAUDE_SESSION_ID`` to an MCP child —
    checked on three live stdio servers, none of which had it — so the
    per-call refresh keyed on ``session_id`` was dead code on the one
    path where the mail actually happens.
    """

    def _tools(self, alias="xollama", sid=""):
        from claude_hooks.mailbox.tools import MailboxTools
        return MailboxTools(self.store, alias=alias, session_id=sid,
                            host=self.HOST)

    def _age(self, sid, minutes):
        stale = utcnow() - timedelta(minutes=minutes)
        with self.db.lock:
            conn = self.db()
            conn.execute("UPDATE session_registry SET last_seen = ?"
                         " WHERE session_id = ?",
                         (stale.isoformat(), sid))
            conn.commit()

    def _last_seen(self, sid):
        rows = [r for r in self.store.sessions(include_stale=True)
                if r.session_id == sid]
        self.assertEqual(len(rows), 1)
        return rows[0].last_seen

    def test_a_tool_call_with_no_session_id_still_refreshes_the_row(self):
        self.register("xollama", self.HOST, sid="registered-by-the-hook")
        self._age("registered-by-the-hook", minutes=15)
        stale = self._last_seen("registered-by-the-hook")

        self._tools().call("mailbox-list", {})

        self.assertGreater(self._last_seen("registered-by-the-hook"), stale)

    def test_it_refreshes_without_creating_a_second_row(self):
        self.register("xollama", self.HOST, sid="registered-by-the-hook")
        self._tools().call("mailbox-list", {})
        rows = self.store.sessions(alias="xollama")
        self.assertEqual([r.session_id for r in rows],
                         ["registered-by-the-hook"])

    def test_an_unregistered_alias_is_not_invented(self):
        """A session that cannot state its id must not be registered:
        the row would have no id anything could later clean up."""
        self.assertFalse(self.store.touch_alias("nobody", self.HOST))
        self._tools(alias="nobody").call("mailbox-list", {})
        self.assertEqual(self.store.sessions(include_stale=True), [])

    def test_another_host_holding_the_alias_is_not_refreshed(self):
        self.register("xollama", self.HOST, sid="here")
        self.register("xollama", "elsewhere", sid="there")
        self._age("there", minutes=45)
        stale = self._last_seen("there")

        self._tools().call("mailbox-list", {})

        self.assertEqual(self._last_seen("there"), stale)

    def test_an_empty_alias_refreshes_nothing(self):
        self.register("xollama", self.HOST, sid="here")
        self.assertFalse(self.store.touch_alias("", self.HOST))


class RefuseIdenticalResendTests(StoreHarness):
    """An identical message is refused, naming the one it repeats.

    Reported 2026-09-22: ``#185``–``#189``, identical bodies, one minute.
    Not a sender sending five times — one ``send()`` in an MCP server
    started 2026-09-17 wrote one row per *registration*, and the fan-out
    fix had landed on 2026-09-19. The writer was five days stale and
    nothing about a long-lived child process says so, which is the
    argument for checking at the destination instead of trusting the
    writer.
    """

    def setUp(self):
        super().setUp()
        self.register("osync", self.HOST, sid="s-osync")

    def _send(self, subject="s", body="b", to="osync"):
        return self.store.send(to, subject, body, from_alias="me")

    def _age_message(self, message_id, *, seconds):
        old = utcnow() - timedelta(seconds=seconds)
        with self.db.lock:
            conn = self.db()
            conn.execute("UPDATE session_messages SET created_at = ?"
                         " WHERE id = ?", (old.isoformat(), message_id))
            conn.commit()

    def _read(self, message_id):
        self.store.read([message_id], reader_session="s-osync",
                        alias="osync", host=self.HOST)

    def test_an_immediate_repeat_is_refused(self):
        first = self._send()["ids"][0]
        with self.assertRaises(MailboxError) as cm:
            self._send()
        msg = str(cm.exception)
        self.assertIn(f"#{first}", msg)
        self.assertIn("Not sent", msg)

    def test_the_refusal_says_the_message_is_already_there(self):
        """The likeliest reader of this text is a caller retrying because
        it never saw the first confirmation. "Rejected" alone would read
        as a failure and invite a third attempt."""
        self._send()
        with self.assertRaises(MailboxError) as cm:
            self._send()
        msg = str(cm.exception)
        self.assertIn("in their mailbox", msg)
        self.assertIn("nothing more is needed", msg.lower())

    def test_nothing_is_written_when_it_is_refused(self):
        self._send()
        before = len(self.store.inbox(alias="osync", include_read=True))
        with self.assertRaises(MailboxError):
            self._send()
        self.assertEqual(
            len(self.store.inbox(alias="osync", include_read=True)), before)

    def test_an_unread_copy_blocks_a_repeat_at_any_age(self):
        """A second copy cannot tell the recipient anything the first,
        still sitting unread, will not."""
        first = self._send()["ids"][0]
        self._age_message(first, seconds=90 * 24 * 3600)
        with self.assertRaises(MailboxError) as cm:
            self._send()
        self.assertIn(f"#{first}", str(cm.exception))

    def test_a_read_copy_outside_the_window_does_not_block(self):
        first = self._send()["ids"][0]
        self._read(first)
        self._age_message(first, seconds=3600)
        self._send()          # a deliberate re-send, delayed not forbidden
        self.assertEqual(
            len(self.store.inbox(alias="osync", include_read=True)), 2)

    def test_a_read_copy_inside_the_window_still_blocks(self):
        """Read is not the same as answered: a retry whose first attempt
        was read in the meantime is still a retry."""
        first = self._send()["ids"][0]
        self._read(first)
        with self.assertRaises(MailboxError):
            self._send()

    def test_a_withdrawn_message_does_not_block(self):
        """Withdrawing is a statement that it should not have been sent,
        so it cannot stand in the way of sending it properly."""
        first = self._send()["ids"][0]
        self.store.cancel(first, from_alias="me", from_host=self.HOST)
        self._send()
        self.assertEqual(len(self.store.inbox(alias="osync")), 1)

    def test_a_different_body_is_not_a_duplicate(self):
        self._send(body="one")
        self._send(body="two")
        self.assertEqual(len(self.store.inbox(alias="osync")), 2)

    def test_a_different_subject_is_not_a_duplicate(self):
        self._send(subject="one")
        self._send(subject="two")
        self.assertEqual(len(self.store.inbox(alias="osync")), 2)

    def test_another_sender_repeating_the_text_is_not_a_duplicate(self):
        """Two sessions independently reporting the same result are two
        messages, and the recipient needs both."""
        self._send()
        self.store.send("osync", "s", "b", from_alias="someone-else")
        self.assertEqual(len(self.store.inbox(alias="osync")), 2)

    def test_the_same_text_to_a_different_recipient_is_not_a_duplicate(self):
        self.register("xollama", self.HOST, sid="s-x")
        self._send(to="osync")
        self._send(to="xollama")
        self.assertEqual(len(self.store.inbox(alias="osync")), 1)
        self.assertEqual(len(self.store.inbox(alias="xollama")), 1)

    def test_a_parked_message_blocks_its_own_repeat(self):
        """Nobody registered, so it parks on the alias — and a retry of a
        parked message duplicates just as well as a delivered one."""
        first = self.store.send("nobody-home", "s", "b",
                                from_alias="me")["ids"][0]
        with self.assertRaises(MailboxError) as cm:
            self.store.send("nobody-home", "s", "b", from_alias="me")
        self.assertIn(f"#{first}", str(cm.exception))

    def test_a_session_addressed_repeat_is_refused(self):
        first = self.store.send("s-osync", "s", "b",
                                from_alias="me")["ids"][0]
        with self.assertRaises(MailboxError) as cm:
            self.store.send("s-osync", "s", "b", from_alias="me")
        self.assertIn(f"#{first}", str(cm.exception))


class PartialBroadcastTests(StoreHarness):
    """A broadcast where only some recipients already have it."""

    def setUp(self):
        super().setUp()
        self.register("osync", "solidpc", sid="s-sol")
        self.register("osync", "pandorum", sid="s-pan")

    def test_only_the_fresh_hosts_are_written(self):
        first = self.store.send("osync@solidpc", "s", "b",
                                from_alias="me")["ids"][0]
        res = self.store.send("osync*", "s", "b", from_alias="me")

        self.assertEqual(len(res["ids"]), 1)
        self.assertEqual(res["destinations"], [("osync", "pandorum")])
        self.assertEqual(res["skipped"], [(("osync", None, "solidpc"), first)])

    def test_a_single_surviving_row_is_not_a_broadcast(self):
        """``broadcast_group`` means "this went to more than one
        mailbox". After the duplicate is dropped it went to one."""
        self.store.send("osync@solidpc", "s", "b", from_alias="me")
        res = self.store.send("osync*", "s", "b", from_alias="me")
        self.assertIsNone(res["broadcast_group"])

    def test_the_confirmation_says_what_was_skipped(self):
        from claude_hooks.mailbox.tools import MailboxTools
        tools = MailboxTools(self.store, alias="me", session_id="s-me",
                            host=self.HOST)
        first = self.store.send("osync@solidpc", "s", "b",
                                from_alias="me")["ids"][0]

        out = tools.call("mailbox-send", {"to": "osync*", "subject": "s",
                                         "body": "b"})

        self.assertIn("Sent", out)
        self.assertIn("Skipped", out)
        self.assertIn(f"#{first}", out)
        self.assertIn("solidpc", out)

    def test_a_fully_duplicate_broadcast_is_refused(self):
        self.store.send("osync*", "s", "b", from_alias="me")
        with self.assertRaises(MailboxError) as cm:
            self.store.send("osync*", "s", "b", from_alias="me")
        msg = str(cm.exception)
        self.assertIn("Every recipient already has", msg)
        self.assertIn("solidpc", msg)
        self.assertIn("pandorum", msg)
