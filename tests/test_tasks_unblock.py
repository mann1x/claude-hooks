"""Closing a task tells the projects it unblocks (ch-28 phase 2).

``task-done`` / ``task-cancel`` list every task, in any project, whose last
open dependency was the one closed, and mail the session working in each
other project (the send's reply carries the doorbell line). Nothing is
written into the other project.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest import mock

from claude_hooks.tasks.notify import _under, project_session
from claude_hooks.tasks.tools import TaskTools
from tests.test_tasks_graph import _Two


@dataclass
class _Session:
    alias: str
    host: str
    cwd: str
    last_seen: str = "2026-11-19 10:00:00+00:00"


class _Store:
    def __init__(self, sessions):
        self._s = sessions

    def sessions(self):
        return list(self._s)


class _Mailer:
    """Stands in for the session's MailboxTools."""

    def __init__(self, sessions, alias="opencoti", host="solidpc"):
        self.store = _Store(sessions)
        self.alias, self.host = alias, host
        self.sent: list[dict] = []

    def call(self, name, args):
        assert name == "mailbox-send"
        self.sent.append(args)
        return (f"Sent (id {len(self.sent)}) — delivered to `{args['to']}`\n"
                f"\nRing the doorbell: …")


class UnblockTests(_Two):
    def setUp(self):
        super().setUp()
        self.mailer = _Mailer([
            _Session("claude-hooks", "solidpc", str(self.ch.dir.root)),
            _Session("claude-hooks", "solidpc", str(self.ch.dir.root),
                     last_seen="2026-11-19 12:00:00+00:00"),
            _Session("elsewhere", "solidpc", str(self.tmp / "other")),
        ])
        self.tools = TaskTools(lambda: self.op, background_embed=False)
        self.tools.mailer_fn = lambda: self.mailer

    def test_closing_the_last_dependency_mails_the_other_project(self):
        self.ch.create("ship after op-1", depends=["op-1"])
        out = self.tools.call("task-done", {"id": "op-1", "note": "fixed"})
        self.assertIn("Ready now: ch-1 (claude-hooks)", out)
        self.assertEqual(len(self.mailer.sent), 1)
        m = self.mailer.sent[0]
        self.assertEqual(m["to"], "claude-hooks@solidpc")
        self.assertIn("op-1 done: ch-1 ready in claude-hooks", m["subject"])
        self.assertIn("- ch-1 · ship after op-1", m["body"])
        self.assertIn("claude-hooks: Sent (id 1)", out)
        self.assertIn("Ring the doorbell", out)

    def test_a_task_still_waiting_on_something_else_is_not_ready(self):
        self.ch.create("needs both", depends=["op-1", "op-2"])
        out = self.tools.call("task-done", {"id": "op-1"})
        self.assertNotIn("Ready now", out)
        self.assertEqual(self.mailer.sent, [])

    def test_same_project_dependents_are_listed_without_mail(self):
        self.op.create("after op-1 here", depends=["op-1"])
        out = self.tools.call("task-done", {"id": "op-1"})
        self.assertIn("Ready now: op-4", out)
        self.assertNotIn("(opencoti)", out)
        self.assertEqual(self.mailer.sent, [])

    def test_a_cancelled_dependency_says_so(self):
        self.ch.create("x", depends=["op-1"])
        out = self.tools.call("task-cancel", {"id": "op-1", "note": "dropped"})
        self.assertIn("op-1 was cancelled, not done", out)
        self.assertIn("cancelled, not done", self.mailer.sent[0]["body"])

    def test_no_registered_session_is_said_not_mailed(self):
        self.mailer.store = _Store([])
        self.ch.create("x", depends=["op-1"])
        out = self.tools.call("task-done", {"id": "op-1"})
        self.assertIn("claude-hooks: no session is registered", out)
        self.assertEqual(self.mailer.sent, [])

    def test_the_switch_turns_the_mail_off(self):
        self.ch.create("x", depends=["op-1"])
        with mock.patch("claude_hooks.tasks.tools._unblock_mail_on",
                        return_value=False):
            out = self.tools.call("task-done", {"id": "op-1"})
        self.assertIn("Ready now: ch-1 (claude-hooks)", out)
        self.assertEqual(self.mailer.sent, [])

    def test_closing_twice_unblocks_nothing_new(self):
        self.ch.create("x", depends=["op-1"])
        self.tools.call("task-done", {"id": "op-1"})
        out = self.tools.call("task-done", {"id": "op-1", "note": "again"})
        self.assertNotIn("Ready now", out)
        self.assertEqual(len(self.mailer.sent), 1)

    def test_nothing_is_written_into_the_other_project(self):
        self.ch.create("x", depends=["op-1"])
        before = (self.ch.dir.dir / "ch-1.md").read_text()
        self.tools.call("task-done", {"id": "op-1"})
        self.assertEqual((self.ch.dir.dir / "ch-1.md").read_text(), before)

    def test_without_a_mailbox_the_reply_says_so(self):
        self.tools.mailer_fn = None
        self.ch.create("x", depends=["op-1"])
        out = self.tools.call("task-done", {"id": "op-1"})
        self.assertIn("no mail sent from here", out)


class SessionLookupTests(_Two):
    def test_the_newest_session_in_the_projects_root_wins(self):
        root = str(self.ch.dir.root)
        store = _Store([
            _Session("old", "solidpc", root, "2026-11-01 00:00:00+00:00"),
            _Session("new", "solidpc", root + "/sub", "2026-11-19 00:00:00+00:00"),
            _Session("pan", "pandorum", root, "2026-11-20 00:00:00+00:00"),
        ])
        self.assertEqual(project_session(store, self.index, "claude-hooks"),
                         ("new", "solidpc"))
        self.assertIsNone(project_session(store, self.index, "nope"))

    def test_under(self):
        self.assertTrue(_under("/a/b", "/a/b"))
        self.assertTrue(_under("/a/b/c", "/a/b/"))
        self.assertFalse(_under("/a/bc", "/a/b"))
        self.assertFalse(_under("", "/a"))



class ServerWiringTests(unittest.TestCase):
    """Both memory servers hand the task tools their mailbox, so a close
    can mail as this session; a tool added to one server goes on both."""

    def test_both_servers_wire_the_mailer(self):
        from claude_hooks.pgvector_mcp.server import McpServer as Pg
        from claude_hooks.sqlite_vec_mcp.server import McpServer as Sq
        from tests.test_pgvector_mcp import FakePgvectorProvider
        from tests.test_sqlite_vec_mcp import FakeSqliteVecProvider
        for cls, prov in ((Pg, FakePgvectorProvider),
                          (Sq, FakeSqliteVecProvider)):
            server = cls(prov())
            tools = server._tasks()
            self.assertIsNotNone(tools, cls.__module__)
            self.assertEqual(tools.mailer_fn, server._mailbox, cls.__module__)


if __name__ == "__main__":
    unittest.main()
