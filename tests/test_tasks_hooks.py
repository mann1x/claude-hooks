"""Task tracking M3: the hooks that keep the list in front of the model."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from claude_hooks.tasks import hook as th
from claude_hooks.tasks.service import TaskService
from claude_hooks.tasks.store import TaskIndex


class _Db:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.RLock()

    def __call__(self):
        return self.conn


def _letters(text):
    v = [0.0] * 26
    for c in text.lower():
        if "a" <= c <= "z":
            v[ord(c) - 97] += 1
    return v


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "backup_models"
        self.root.mkdir()
        db = _Db(self.tmp / "t.db")
        self.addCleanup(db.conn.close)
        self.index = TaskIndex(db, db.lock, dialect="sqlite")
        self.svc = TaskService(self.root, self.index, host="h",
                               session_id="sess1234", embedder=_letters,
                               embed_model="letters")
        p = mock.patch.object(th, "_service",
                              lambda *a, **k: self._fresh())
        p.start()
        self.addCleanup(p.stop)
        self.config = {}
        self.event = {"session_id": "sess1234", "cwd": str(self.root)}

    def _fresh(self):
        return TaskService(self.root, self.index, host="h",
                           session_id="sess1234", embedder=_letters,
                           embed_model="letters", embed_on_write=False)


class SessionBlockTests(_Base):
    def test_hint_before_any_task(self):
        out = th.session_block(event=self.event, config=self.config,
                               providers=[])
        self.assertIn("no task list in backup_models yet", out)

    def test_lists_active_then_ready(self):
        a = self.svc.create("running job", priority="H")
        self.svc.set_status(a.id, "active")
        for i in range(7):
            self.svc.create(f"later {i}")
        out = th.session_block(event=self.event, config=self.config,
                               providers=[])
        self.assertIn("**Tasks (backup_models):** 1 active · 7 ready", out)
        self.assertLess(out.index("bm-1"), out.index("bm-2"))
        self.assertIn("… 2 more ready", out)
        self.assertIn("TASKS.md", out)

    def test_can_be_turned_off(self):
        self.svc.create("x")
        cfg = {"hooks": {"tasks": {"session_start": False}}}
        self.assertEqual(th.session_block(event=self.event, config=cfg,
                                          providers=[]), "")


class PromptTests(_Base):
    def test_mentions(self):
        self.svc.create("zebra stripes", description="count them")
        self.svc.note("bm-1", "counted 42")
        ev = dict(self.event, prompt="what about bm-1 and xx-3?")
        out = th.prompt_block(event=ev, config=self.config, providers=[])
        self.assertIn("bm-1 [pending] zebra stripes", out)
        self.assertIn("count them", out)
        self.assertIn("counted 42", out)
        self.assertNotIn("xx-3", out)

    def test_mention_regex(self):
        ids = th.mentioned_ids("bm-1, BM-2; abc-bm-3 bm-4x (bm-1)", {"bm"})
        self.assertEqual(ids, ["bm-1", "bm-2"])

    def test_semantic_recall(self):
        self.svc.create("zzz zebra zoo")
        self.svc.create("aaa apple")
        ev = dict(self.event, prompt="tell me about the zebra in the zoo "
                                     "please, zzz")
        # Letter-count vectors make all English alike; the threshold is
        # what separates them here.
        cfg = {"hooks": {"tasks": {"recall_min_score": 0.8}}}
        out = th.prompt_block(event=ev, config=cfg, providers=[])
        self.assertIn("bm-1", out)
        self.assertNotIn("bm-2", out)

    def test_prompt_recall_overlaps(self):
        self.svc.create("zzz zebra")
        ev = dict(self.event, prompt="bm-1 please")
        r = th.PromptRecall(event=ev, config=self.config,
                            providers=[]).start()
        self.assertIn("bm-1", r.join())

    def test_nothing_for_uninitialised_project(self):
        ev = dict(self.event, prompt="bm-1 what is this about, really now")
        self.assertEqual(th.prompt_block(event=ev, config=self.config,
                                         providers=[]), "")


def _transcript(path: Path, tools: list[dict]) -> str:
    rows = [{"type": "user", "message": {"content": "do the thing"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"t{i}", "name": t["name"],
                 "input": t.get("input", {})} for i, t in enumerate(tools)]}}]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return str(path)


class StopNudgeTests(_Base):
    def _event(self, tools):
        return dict(self.event, transcript_path=_transcript(
            self.tmp / "t.jsonl", tools))

    def test_nudges_once_per_change(self):
        a = self.svc.create("job")
        self.svc.set_status(a.id, "active")
        ev = self._event([{"name": "Edit",
                           "input": {"file_path": "/x/a.py"}}])
        reason = th.stop_nudge(event=ev, config=self.config, providers=[])
        self.assertIn("bm-1 (job) is active", reason)
        self.assertIsNone(th.stop_nudge(event=ev, config=self.config,
                                        providers=[]))
        ev2 = self._event([{"name": "Write",
                            "input": {"file_path": "/x/b.py"}}])
        self.assertIsNotNone(th.stop_nudge(event=ev2, config=self.config,
                                           providers=[]))

    def test_quiet_when_a_task_tool_ran(self):
        self.svc.set_status(self.svc.create("job").id, "active")
        ev = self._event([{"name": "Edit", "input": {"file_path": "/a"}},
                          {"name": "mcp__pgvector__task-note"}])
        self.assertIsNone(th.stop_nudge(event=ev, config=self.config,
                                        providers=[]))

    def test_quiet_without_edits_active_or_on_continuation(self):
        ev = self._event([{"name": "Edit", "input": {"file_path": "/a"}}])
        self.svc.create("not active")
        self.assertIsNone(th.stop_nudge(event=ev, config=self.config,
                                        providers=[]))
        self.svc.set_status("bm-1", "active")
        self.assertIsNone(th.stop_nudge(
            event=dict(ev, stop_hook_active=True), config=self.config,
            providers=[]))
        ev_read = self._event([{"name": "Read", "input": {"file_path": "/a"}}])
        self.assertIsNone(th.stop_nudge(event=ev_read, config=self.config,
                                        providers=[]))

    def test_joins_an_existing_block(self):
        from claude_hooks.hooks.stop import _with_task_nudge
        self.svc.set_status(self.svc.create("job").id, "active")
        ev = self._event([{"name": "Edit", "input": {"file_path": "/a"}}])
        out = _with_task_nudge({"decision": "block", "reason": "mail!"},
                               ev, self.config, [])
        self.assertTrue(out["reason"].startswith("mail!\n\n"))
        self.assertIn("Task tracking", out["reason"])


class MirrorTests(_Base):
    def test_create_and_update(self):
        ev = dict(self.event, tool_name="TaskCreate",
                  tool_input={"subject": "Built-in", "description": "d"},
                  tool_response={"content": "Task #7 created successfully"})
        th.mirror_builtin(event=ev, config=self.config, providers=[])
        t, _ = self.svc.show("bm-1")
        self.assertEqual(t.title, "Built-in")
        self.assertIn("mirrored from Claude Code task #7", t.log_lines[0])
        ev = dict(self.event, tool_name="TaskUpdate",
                  tool_input={"taskId": "7", "status": "completed"})
        th.mirror_builtin(event=ev, config=self.config, providers=[])
        self.assertEqual(self.svc.show("bm-1")[0].status, "done")

    def test_unknown_update_is_ignored(self):
        ev = dict(self.event, tool_name="TaskUpdate",
                  tool_input={"taskId": "9", "status": "completed"})
        th.mirror_builtin(event=ev, config=self.config, providers=[])
        self.assertFalse(self.svc.dir.exists)


class HookPartsTests(_Base):
    def test_tasks_part_routes_every_event(self):
        from claude_hooks import hook_parts
        self.svc.set_status(self.svc.create("job").id, "active")
        keep = hook_parts.parse_marker("keep: tasks")
        self.assertEqual(keep, frozenset({"tasks"}))
        out = hook_parts.run("SessionStart", event=self.event,
                             config=self.config, providers=[], keep=keep)
        self.assertIn("bm-1",
                      out["hookSpecificOutput"]["additionalContext"])
        out = hook_parts.run("UserPromptSubmit",
                             event=dict(self.event, prompt="bm-1?"),
                             config=self.config, providers=[], keep=keep)
        self.assertIn("job", out["hookSpecificOutput"]["additionalContext"])
        ev = dict(self.event, transcript_path=_transcript(
            self.tmp / "t.jsonl", [{"name": "Edit",
                                    "input": {"file_path": "/a"}}]))
        out = hook_parts.run("Stop", event=ev, config=self.config,
                             providers=[], keep=keep)
        self.assertEqual(out["decision"], "block")
        hook_parts.run("PostToolUse", event=dict(
            self.event, tool_name="TaskCreate",
            tool_input={"subject": "via marker"},
            tool_response="Task #1 created"), config=self.config,
            providers=[], keep=keep)
        self.assertEqual(self.svc.show("bm-2")[0].title, "via marker")

    def test_other_parts_do_not_run_tasks(self):
        from claude_hooks import hook_parts
        self.svc.create("job")
        out = hook_parts.run("SessionStart", event=self.event,
                             config=self.config, providers=[],
                             keep=frozenset({"guards"}))
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
