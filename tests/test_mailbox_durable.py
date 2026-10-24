"""Durable aliases (ch-18): an address that lasts without a session.

A cloud thread linked to a machine acts as that machine's session and has
no session of its own to stay alive with, so the alias it took lapsed
with the 24 h eviction; the file mailbox on pandorum had nothing
registered between sessions. A durable registration is kept while quiet,
survives its session's end, is handed to whoever takes its slot next,
and belongs to the ``alias@host`` that registered it.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.mailbox import MailboxError  # noqa: E402
from claude_hooks.mailbox.store import utcnow  # noqa: E402
from tests.test_mailbox import StoreHarness  # noqa: E402

OWNER = "claude-hooks@solidpc"


class _Durable(StoreHarness):

    def _set(self, session_id, **cols):
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self.db.lock:
            conn = self.db()
            conn.cursor().execute(
                f"UPDATE session_registry SET {sets} WHERE session_id = ?",
                (*cols.values(), session_id))
            conn.commit()

    def _age(self, session_id, hours):
        self._set(session_id, last_seen=self.store._at(
            utcnow() - timedelta(hours=hours)))

    def _row(self, alias, host):
        rows = [s for s in self.store.sessions(include_stale=True)
                if (s.alias, s.host) == (alias, host)]
        return rows[0] if rows else None


class DurableStoreTests(_Durable):

    def test_register_makes_a_listed_owned_row(self):
        res = self.store.register_durable("osync", "cloud", owner=OWNER)
        self.assertEqual(res["session_id"], "cloud-osync")
        self.assertFalse(res["renewed"])
        row = self._row("osync", "cloud")
        self.assertTrue(row.durable)
        self.assertEqual(row.owner, OWNER)

    def test_a_quiet_durable_alias_stays_an_addressee(self):
        self.store.register_durable("osync", "cloud", owner=OWNER)
        self._age("cloud-osync", 72)
        self.assertEqual([s.label() for s in self.store.sessions()],
                         ["osync@cloud"])
        self.assertEqual(self.store.evict_stale(), 0)
        self.assertEqual(self.store.sweep_registry(days=1), 0)
        # A bare alias resolves to it, so mail to `osync` reaches it.
        res = self.store.send("osync", "s", "b", from_alias="x")
        self.assertEqual(res["recipients"][0].host, "cloud")

    def test_an_ordinary_quiet_row_is_still_evicted(self):
        self.register("plain", self.HOST, sid="plain")
        self._age("plain", 72)
        self.assertEqual(self.store.evict_stale(), 1)

    def test_a_passed_expiry_makes_it_ordinary_again(self):
        self.store.register_durable("osync", "cloud", owner=OWNER,
                                    until=utcnow() + timedelta(hours=1))
        self._set("cloud-osync", durable_until=self.store._at(
            utcnow() - timedelta(minutes=1)))
        self._age("cloud-osync", 72)
        self.assertEqual(self.store.sessions(), [])
        self.assertIsNone(self.store.owned("osync", "cloud", OWNER))
        self.assertEqual(self.store.evict_stale(), 1)
        self.assertIsNone(self._row("osync", "cloud"))

    def test_an_expiry_in_the_past_is_refused(self):
        with self.assertRaises(MailboxError):
            self.store.register_durable(
                "osync", "cloud", owner=OWNER,
                until=utcnow() - timedelta(seconds=1))

    def test_registering_again_renews(self):
        self.store.register_durable("osync", "cloud", owner=OWNER,
                                    until=utcnow() + timedelta(hours=1))
        res = self.store.register_durable("osync", "cloud", owner=OWNER)
        self.assertTrue(res["renewed"])
        self.assertIsNone(self.store.durable_rows()[0]["durable_until"])

    def test_session_end_keeps_the_slot(self):
        self.store.register_durable("box", self.HOST, owner=OWNER)
        got = self.store.claim("s1", "box", host=self.HOST,
                               client=(4242, 1.0))
        self.assertEqual(got["alias"], "box")
        self.assertTrue(self.store.forget("s1"))
        row = self._row("box", self.HOST)
        self.assertIsNotNone(row)
        self.assertTrue(row.durable)
        self.assertEqual(row.owner, OWNER)

    def test_a_session_takes_an_idle_durable_slot_by_its_name(self):
        """Not `box-2`: a durable row with no process is an address
        waiting for a session, not a live session."""
        self.store.register_durable("box", self.HOST, owner=OWNER)
        got = self.store.claim("s1", "box", host=self.HOST,
                               client=(4242, 1.0))
        self.assertEqual(got["alias"], "box")
        row = self._row("box", self.HOST)
        self.assertEqual(row.session_id, "s1")
        self.assertTrue(row.durable)

    def test_register_over_a_durable_slot_keeps_it(self):
        """The file relay's `mailbox-durable` request goes through
        register(); the slot it re-registers stays durable."""
        self.store.register_durable("osync", "cloud", owner=OWNER)
        self.store.register("cloud-osync", "osync", host="cloud")
        self.assertTrue(self._row("osync", "cloud").durable)
        self.store.register("other-sid", "osync", host="cloud")
        row = self._row("osync", "cloud")
        self.assertEqual(row.session_id, "other-sid")
        self.assertTrue(row.durable)
        self.assertEqual(row.owner, OWNER)

    def test_touch_rebuilding_an_evicted_row_keeps_it_durable(self):
        self.store.register_durable("osync", "cloud", owner=OWNER)
        self.store.touch("new-sid", alias="osync", host="cloud")
        self.assertTrue(self._row("osync", "cloud").durable)

    def test_another_owner_is_refused_unless_forced(self):
        self.store.register_durable("osync", "cloud", owner=OWNER)
        with self.assertRaises(MailboxError) as cm:
            self.store.register_durable("osync", "cloud",
                                        owner="other@pandorum")
        self.assertIn(OWNER, str(cm.exception))
        self.store.register_durable("osync", "cloud",
                                    owner="other@pandorum", force=True)
        self.assertEqual(self._row("osync", "cloud").owner, "other@pandorum")

    def test_a_live_session_keeps_its_alias(self):
        self.register("busy", "cloud", sid="relay-other")
        with self.assertRaises(MailboxError) as cm:
            self.store.register_durable("busy", "cloud", owner=OWNER)
        self.assertIn("live session", str(cm.exception))

    def test_a_stale_session_slot_is_taken(self):
        self.register("old", "cloud", sid="gone")
        self._age("gone", 48)
        self.store.register_durable("old", "cloud", owner=OWNER)
        self.assertTrue(self._row("old", "cloud").durable)

    def test_an_alias_live_on_another_host_is_always_refused(self):
        """A second `osync` would make a bare `osync` ambiguous, and
        every sender's mail to it refused — force does not change that."""
        self.register("osync", self.HOST, sid="local-osync")
        for force in (False, True):
            with self.subTest(force=force):
                with self.assertRaises(MailboxError) as cm:
                    self.store.register_durable("osync", "cloud",
                                                owner=OWNER, force=force)
                self.assertIn("ambiguous", str(cm.exception))

    def test_a_bad_alias_is_refused(self):
        for bad in ("", "a b", "-x", "x@y"):
            with self.subTest(alias=bad):
                with self.assertRaises(MailboxError):
                    self.store.register_durable(bad, "cloud", owner=OWNER)

    def test_release_is_the_owners_and_keeps_the_mail(self):
        self.store.register_durable("osync", "cloud", owner=OWNER)
        self.store.send("osync@cloud", "s", "b", from_alias="x")
        with self.assertRaises(MailboxError):
            self.store.release_durable("osync", "cloud",
                                       owner="other@pandorum")
        self.assertTrue(self.store.release_durable("osync", "cloud",
                                                   owner=OWNER))
        self.assertIsNone(self._row("osync", "cloud"))
        self.assertFalse(self.store.release_durable("osync", "cloud",
                                                    owner=OWNER))
        self.assertEqual(self.store.inbox_count(alias="osync", host="cloud"),
                         1)

    def test_release_under_a_running_session_keeps_its_row(self):
        self.store.register_durable("box", self.HOST, owner=OWNER)
        self.store.claim("s1", "box", host=self.HOST, client=(4242, 1.0))
        self.store.release_durable("box", self.HOST, owner=OWNER)
        row = self._row("box", self.HOST)
        self.assertEqual(row.session_id, "s1")
        self.assertFalse(row.durable)


class DurableToolsTests(_Durable):

    def setUp(self):
        super().setUp()
        from claude_hooks.mailbox.tools import MailboxTools
        self.register("claude-hooks", self.HOST, sid="s-ch")
        self.register("xollama", self.HOST, sid="s-xo")
        self.me = MailboxTools(self.store, alias="claude-hooks",
                               session_id="s-ch", host=self.HOST)
        self.peer = MailboxTools(self.store, alias="xollama",
                                 session_id="s-xo", host=self.HOST)

    def call(self, tools, name, **args):
        return tools.call(name, args, headless=False)

    def test_register_send_as_it_and_read_the_reply(self):
        out = self.call(self.me, "mailbox-durable", action="register",
                        alias="osync")
        self.assertIn("Registered osync@cloud", out)
        out = self.call(self.me, "mailbox-send", to="xollama", subject="q",
                        body="from osync", **{"from": "osync@cloud"})
        self.assertIn("Sent", out)
        listing = self.call(self.peer, "mailbox-list")
        self.assertIn("from osync@cloud", listing)
        # The reply goes to osync@cloud's inbox, not claude-hooks'.
        self.call(self.peer, "mailbox-send", to="osync", subject="re: q",
                  body="answer")
        self.assertIn("No messages", self.call(self.me, "mailbox-list")
                      .split("\n\n")[0])
        mine = self.call(self.me, "mailbox-list", **{"as": "osync"})
        self.assertIn("'re: q'", mine)
        mid = int(mine.split("#")[1].split()[0])
        read = self.call(self.me, "mailbox-read", ids=[mid],
                         **{"as": "osync@cloud"})
        self.assertIn("answer", read)
        self.assertIn("Marked read", read)
        sent = self.call(self.me, "mailbox-sent", **{"as": "osync"})
        self.assertIn("→ xollama", sent)

    def test_own_listing_names_the_durable_inbox_waiting(self):
        self.call(self.me, "mailbox-durable", action="register", alias="osync")
        self.call(self.peer, "mailbox-send", to="osync@cloud", subject="s",
                  body="b")
        out = self.call(self.me, "mailbox-list")
        self.assertIn("Unread for your durable alias(es): osync@cloud (1)",
                      out)

    def test_only_the_owner_acts_as_it(self):
        self.call(self.me, "mailbox-durable", action="register", alias="osync")
        out = self.call(self.peer, "mailbox-send", to="claude-hooks",
                        subject="s", body="b", **{"from": "osync"})
        self.assertIn("not a durable alias of xollama@solidpc", out)
        out = self.call(self.peer, "mailbox-list", **{"as": "osync"})
        self.assertIn("not a durable alias", out)
        out = self.call(self.peer, "mailbox-durable", action="remove",
                        alias="osync")
        self.assertIn("only its owner", out)

    def test_as_yourself_is_just_you(self):
        out = self.call(self.me, "mailbox-list",
                        **{"as": f"claude-hooks@{self.HOST}"})
        self.assertIn("No messages for claude-hooks", out)

    def test_another_hosts_alias_is_refused(self):
        out = self.call(self.me, "mailbox-durable", action="register",
                        alias="box@pandorum")
        self.assertIn("not pandorum", out)
        out = self.call(self.me, "mailbox-durable", action="register",
                        alias=f"box@{self.HOST}")
        self.assertIn(f"Registered box@{self.HOST}", out)

    def test_a_live_local_alias_cannot_be_taken(self):
        out = self.call(self.me, "mailbox-durable", action="register",
                        alias="xollama")
        self.assertIn("ambiguous", out)

    def test_ttl(self):
        out = self.call(self.me, "mailbox-durable", action="register",
                        alias="osync", ttl="30d")
        self.assertIn("until 20", out)
        self.assertIn("ttl", self.call(self.me, "mailbox-durable",
                                       action="register", alias="o2",
                                       ttl="soon"))

    def test_list_sessions_and_remove(self):
        self.call(self.me, "mailbox-durable", action="register", alias="osync")
        self.assertIn("osync@cloud — no expiry, 0 unread",
                      self.call(self.me, "mailbox-durable"))
        self.assertIn(f"[durable, {OWNER}]",
                      self.call(self.me, "mailbox-sessions"))
        self.assertIn("Removed osync@cloud",
                      self.call(self.me, "mailbox-durable", action="remove",
                                alias="osync"))
        self.assertIn("owns no durable aliases",
                      self.call(self.me, "mailbox-durable"))

    def test_a_sent_message_carries_the_durable_identity(self):
        self.call(self.me, "mailbox-durable", action="register", alias="osync")
        self.call(self.me, "mailbox-send", to="xollama", subject="s",
                  body="b", **{"from": "osync"})
        with self.db.lock:
            row = self.db().cursor().execute(
                "SELECT from_alias, from_host, from_session "
                "FROM session_messages").fetchone()
        self.assertEqual(row, ("osync", "cloud", "cloud-osync"))


class AliasesCliTests(_Durable):

    def run_cli(self, *argv):
        from claude_hooks.mailbox import aliases
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = aliases.main(list(argv), store=self.store)
        return rc, buf.getvalue()

    def test_register_list_remove_for_another_host(self):
        rc, out = self.run_cli("register", "claude-mailbox@pandorum")
        self.assertEqual(rc, 0, out)
        self.assertIn("owner claude-mailbox@pandorum", out)
        rc, out = self.run_cli("list")
        self.assertIn("claude-mailbox@pandorum", out)
        rc, out = self.run_cli("remove", "claude-mailbox@pandorum")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.store.durable_rows(), [])

    def test_owner_and_ttl(self):
        rc, out = self.run_cli("register", "osync", "--owner", OWNER,
                               "--ttl", "2w")
        self.assertEqual(rc, 0, out)
        self.assertIn("until", out)
        self.assertIsNotNone(self.store.owned("osync", "cloud", OWNER))

    def test_a_refusal_exits_1(self):
        self.register("busy", "cloud", sid="relay-other")
        rc, out = self.run_cli("register", "busy")
        self.assertEqual(rc, 1)
        self.assertIn("refused", out)
        rc, out = self.run_cli("remove", "nothing")
        self.assertEqual(rc, 1)
