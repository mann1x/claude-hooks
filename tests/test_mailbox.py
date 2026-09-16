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


class StoreHarness(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = _SqliteConn(Path(self._tmp.name) / "m.db")
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
        out = self.me.call("mailbox-sessions", {"os": "windows"})
        self.assertIn("No sessions", out)

    def test_read_with_no_ids(self):
        self.assertIn("Pass ids", self.them.call("mailbox-read", {}))

    def test_handles_reports_ownership(self):
        self.assertTrue(self.me.handles("mailbox-send"))
        self.assertFalse(self.me.handles("pgvector-find"))
