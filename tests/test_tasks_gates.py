"""Gates: a task that waits on something outside the task list
(ch-28 phase 3, docs/PLAN-task-graph.md).

Gates live in the task's ``links`` under ``gates``; an open gate blocks
like an open dependency; a resolved one is removed and logged. Mail,
timer and commit gates are checked at session start, GitHub ones only on
request; one that fails for good is reported, never resolved.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from claude_hooks.tasks.gates import Checker, normalise, parse
from claude_hooks.tasks.model import Task
from claude_hooks.tasks.tools import TaskTools
from tests.test_tasks_graph import _Two


class _MailStore:
    def __init__(self, messages):
        self.messages = messages

    def message_state(self, mid):
        return self.messages.get(mid)


class ParseTests(unittest.TestCase):
    def test_well_formed_gates_normalise(self):
        self.assertEqual(normalise("mail:#2331"), "mail:2331")
        self.assertEqual(normalise("MAIL: 7"), "mail:7")
        self.assertEqual(normalise("timer:2026-11-20T10:00Z"),
                         "timer:2026-11-20T10:00:00Z")
        self.assertEqual(normalise("commit:ABC1234@main"),
                         "commit:abc1234@main")
        self.assertEqual(parse("gh-pr:mann1x/claude-hooks#42"),
                         ("gh-pr", "mann1x/claude-hooks#42"))
        self.assertEqual(parse("human:the owner says deploy"),
                         ("human", "the owner says deploy"))

    def test_malformed_gates_are_refused(self):
        for bad in ("mail:abc", "deploy", "timer:soon", "commit:xyz@main",
                    "commit:abc1234", "gh-pr:42", "nope:1", "human:"):
            with self.assertRaises(ValueError, msg=bad):
                parse(bad)


class CheckerTests(unittest.TestCase):
    def test_timer(self):
        c = Checker(now=datetime(2026, 11, 20, 11, tzinfo=timezone.utc))
        self.assertTrue(c.check("timer:2026-11-20T10:00Z").resolved)
        self.assertFalse(c.check("timer:2026-11-21T10:00Z").resolved)

    def test_mail_answered_by_ack_or_reply(self):
        store = _MailStore({
            1: {"to_alias": "opencoti", "read_at": "x", "ack_body": "ok"},
            2: {"to_alias": "opencoti", "read_at": "x", "reply_id": 9},
            3: {"to_alias": "opencoti", "read_at": "x"},
            4: {"to_alias": "opencoti"},
            5: {"to_alias": "opencoti", "cancelled_at": "x"},
        })
        c = Checker(mail_store=store)
        self.assertTrue(c.check("mail:1").resolved)
        self.assertIn("in #9", c.check("mail:2").why)
        self.assertEqual(c.check("mail:3").why, "mail #3 read, not answered")
        self.assertEqual(c.check("mail:4").why, "mail #4 unread")
        v = c.check("mail:5")
        self.assertTrue(v.failed)
        self.assertFalse(v.resolved)
        self.assertTrue(c.check("mail:6").failed)            # no such mail
        self.assertIsNone(Checker().check("mail:1").resolved)  # no mailbox

    def test_human_waits_for_a_person(self):
        self.assertFalse(Checker().check("human:deploy").resolved)

    def test_github_gates_are_skipped_at_session_start(self):
        c = Checker(run=lambda cmd: (_ for _ in ()).throw(AssertionError))
        v = c.check("gh-pr:a/b#1", cheap_only=True)
        self.assertIsNone(v.resolved)

    def test_github_pr_and_run_states(self):
        answers = {"pr": '{"state": "MERGED"}',
                   "run": '{"status": "completed", "conclusion": "failure"}'}

        def run(cmd):
            return 0, answers[cmd[1]]
        c = Checker(run=run)
        if shutil.which("gh") is None:
            self.skipTest("gh is not installed")
        self.assertTrue(c.check("gh-pr:a/b#1").resolved)
        v = c.check("gh-run:a/b#2")
        self.assertTrue(v.failed)
        answers["pr"] = '{"state": "CLOSED"}'
        self.assertTrue(c.check("gh-pr:a/b#1").failed)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class CommitGateTests(unittest.TestCase):
    def test_a_commit_on_the_branch_opens_the_gate(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)

            def git(*a):
                return subprocess.run(
                    ["git", "-c", "user.name=t",
                     "-c", "user.email=t@example.invalid", *a], cwd=root,
                    check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q", "-b", "main")
            (root / "f").write_text("1")
            git("add", "f")
            git("commit", "-q", "-m", "one")
            git("checkout", "-q", "-b", "side")
            (root / "f").write_text("2")
            git("commit", "-q", "-am", "two")
            sha = git("rev-parse", "HEAD")
            c = Checker(root=root)
            self.assertFalse(c.check(f"commit:{sha}@main").resolved)
            self.assertTrue(c.check(f"commit:{sha}@side").resolved)
            self.assertIsNone(c.check("commit:0000000@main").resolved)


class ServiceGateTests(_Two):
    def test_a_gate_blocks_until_resolved_by_hand(self):
        t = self.ch.create("deploy after the owner's go")
        self.ch.gate(t.id, "add", "human:owner says deploy")
        row = self.ch.board()["blocked"][0]
        self.assertEqual(row["open_gates"], ["human:owner says deploy"])
        self.ch.gate(t.id, "resolve", "human:owner says deploy", "said go")
        self.assertEqual([r["id"] for r in self.ch.board()["ready"]], [t.id])
        task = self.ch.show(t.id)[0]
        self.assertNotIn("gates", task.links)
        self.assertIn("resolved: said go", task.log_lines[0])

    def test_resolving_a_gate_it_lacks_is_refused(self):
        t = self.ch.create("x")
        with self.assertRaises(ValueError):
            self.ch.gate(t.id, "resolve", "mail:1")
        with self.assertRaises(ValueError):
            self.ch.gate(t.id, "add", "bogus")

    def test_check_records_what_opened_and_logs_a_stuck_gate_once(self):
        a = self.ch.create("waits for mail")
        b = self.ch.create("waits for cancelled mail")
        self.ch.gate(a.id, "add", "mail:1")
        self.ch.gate(b.id, "add", "mail:2")
        store = _MailStore({1: {"to_alias": "op", "read_at": "x",
                                "ack_body": "done"},
                            2: {"to_alias": "op", "cancelled_at": "x"}})
        res = self.ch.check_gates(cheap_only=True, mail_store=store)
        self.assertEqual({(t, v.resolved) for t, _, v in res},
                         {(a.id, True), (b.id, False)})
        self.assertNotIn("gates", self.ch.show(a.id)[0].links)
        self.ch.check_gates(cheap_only=True, mail_store=store)
        stuck = [ln for ln in self.ch.show(b.id)[0].log_lines
                 if "will not open by itself" in ln]
        self.assertEqual(len(stuck), 1)

    def test_a_gated_dependent_is_not_reported_unblocked(self):
        t = self.ch.create("needs op-1 and a go", depends=["op-1"])
        self.ch.gate(t.id, "add", "human:go")
        tools = TaskTools(lambda: self.op, background_embed=False)
        out = tools.call("task-done", {"id": "op-1"})
        self.assertNotIn("Ready now", out)

    def test_old_files_without_gates_and_unknown_link_keys_round_trip(self):
        text = ("---\nid: ch-9\ntitle: t\nstatus: pending\npriority: M\n"
                "links: {gates: [mail:5], future: [x]}\ncreated: \n"
                "updated: \n---\n")
        task = Task.from_markdown(text)
        self.assertEqual(task.links["gates"], ["mail:5"])
        self.assertIn("future: [x]", task.to_markdown())


class GateToolTests(_Two):
    def test_add_check_and_resolve_through_the_tool(self):
        tools = TaskTools(lambda: self.ch, background_embed=False)
        self.ch.create("x")
        out = tools.call("task-gate", {"id": "ch-1", "action": "add",
                                       "gate": "timer:2020-01-01T00:00Z"})
        self.assertIn("waits on gate timer:2020-01-01T00:00:00Z", out)
        out = tools.call("task-gate", {"action": "check"})
        self.assertIn("ch-1 timer:2020-01-01T00:00:00Z: open", out)
        self.assertEqual(tools.call("task-gate", {"action": "check"}),
                         "No open gates.")
        out = tools.call("task-gate", {"id": "ch-1", "action": "nope",
                                       "gate": "human:x"})
        self.assertTrue(out.startswith("Not done"))


class SessionStartTests(_Two):
    def test_a_past_timer_opens_at_session_start(self):
        import os
        from unittest import mock

        from claude_hooks.tasks.hook import session_block
        t = self.ch.create("after the cooldown")
        self.ch.gate(t.id, "add", "timer:2020-01-01T00:00Z")
        with mock.patch.dict(os.environ,
                             {"CLAUDE_PROJECT_DIR": str(self.ch.dir.root)}):
            out = session_block(event={"session_id": "s"}, config={},
                                providers=[])
        self.assertIn("Gates opened since last time: ch-1 "
                      "timer:2020-01-01T00:00:00Z", out)
        self.assertNotIn("gates", self.ch.show(t.id)[0].links)


if __name__ == "__main__":
    unittest.main()
