"""Task tracking M2/M4/M5: the MCP tools, the CLI, TASKS.md and the
Claude Code transcript importer."""
from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from claude_hooks.tasks.board import HEADER, write_board
from claude_hooks.tasks.importer import replay, run_import
from claude_hooks.tasks.model import Task
from claude_hooks.tasks.service import TaskService
from claude_hooks.tasks.store import TaskIndex
from claude_hooks.tasks.tools import TOOL_NAMES, TaskTools, tool_catalog


class _Db:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.RLock()

    def __call__(self):
        return self.conn


class _Base(unittest.TestCase):
    with_index = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "backup_models"
        self.root.mkdir()
        self.index = None
        if self.with_index:
            db = _Db(self.tmp / "t.db")
            self.addCleanup(db.conn.close)
            self.index = TaskIndex(db, db.lock, dialect="sqlite")
        self.svc = TaskService(self.root, self.index, host="h",
                               session_id="sess1234")
        self.tools = TaskTools(lambda: self.svc, background_embed=False)

    def call(self, name, **args):
        return self.tools.call(name, args)


class CatalogTests(unittest.TestCase):
    def test_names_match_catalog(self):
        self.assertEqual(tuple(t["name"] for t in tool_catalog()),
                         TOOL_NAMES)
        for t in tool_catalog():
            self.assertEqual(t["inputSchema"]["type"], "object")


class ToolTests(_Base):
    def test_lifecycle(self):
        out = self.call("task-create", title="Import tasks", priority="H",
                        acceptance=["dry run", "real run"], start=True)
        self.assertIn("bm-1 [active H]", out)
        self.assertIn(".claude-hooks/tasks/bm-1.md", out.replace("\\", "/"))
        self.assertIn("bm-1", self.call("task-ready"))
        out = self.call("task-update", id="bm-1", check=[1], note="dry ok")
        self.assertIn("dry ok", out)
        self.assertIn("noted on bm-1", self.call("task-note", id="bm-1",
                                                 text="849 found"))
        self.assertIn("[done H]", self.call("task-done", id="BM-1",
                                            note="imported"))
        self.assertIn("No tasks match", self.call("task-list"))
        self.assertIn("bm-1", self.call("task-list", status="closed"))
        shown = self.call("task-show", id="bm-1")
        self.assertIn("archive", shown)
        self.assertIn("- [x] dry run", shown)

    def test_errors_are_answers(self):
        self.assertIn("is not a task id", self.call("task-done", id="42x"))
        self.assertIn("no task bm-9", self.call("task-show", id="bm-9"))
        self.assertIn("Not done", self.call("task-create", title=" "))
        self.assertIn("unknown status",
                      self.call("task-list", status="someday"))

    def test_ready_before_any_task(self):
        self.assertIn("No task list yet", self.call("task-ready"))

    def test_list_filters_and_pages(self):
        for i in range(25):
            self.call("task-create", title=f"t{i}", area="R9" if i % 2
                      else "R10")
        out = self.call("task-list")
        self.assertIn("25 task(s), page 1 of 2 — next: page=2", out)
        self.assertIn("12 task(s)", self.call("task-list", area="R9"))
        self.assertIn("1 task(s)", self.call("task-list", query="t13"))

    def test_link(self):
        self.call("task-create", title="x")
        self.assertIn("linked commit abc",
                      self.call("task-link", id="bm-1", kind="commit",
                                value="abc"))

    def test_all_projects(self):
        self.call("task-create", title="here")
        other = TaskService(self.tmp / "opencoti", self.index, host="h")
        (self.tmp / "opencoti").mkdir()
        other.create("there")
        out = self.call("task-list", project="all")
        self.assertIn("backup_models:bm-1", out)
        self.assertIn("opencoti:op-1", out)


class FilesOnlyToolTests(ToolTests):
    with_index = False

    def test_list_filters_and_pages(self):
        for i in range(25):
            self.call("task-create", title=f"t{i}")
        self.assertIn("25 task(s), page 1 of 2", self.call("task-list"))

    def test_all_projects(self):
        self.call("task-create", title="here")
        self.assertIn("needs the SQL store",
                      self.call("task-list", project="all"))


class BoardTests(_Base):
    def test_board_is_written_and_linked(self):
        a = self.svc.create("base", plan="docs/plan.md")
        self.svc.create("next", depends=[a.id], plan="docs/plan.md")
        c = self.svc.create("old")
        self.svc.set_status(c.id, "done")
        text = (self.root / "TASKS.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(HEADER))
        self.assertIn("## Ready (1)", text)
        self.assertIn("## Blocked (1)", text)
        self.assertIn("after bm-1", text)
        self.assertIn("[bm-1](.claude-hooks/tasks/bm-1.md)", text)
        self.assertIn("[bm-3](.claude-hooks/tasks/archive/bm-3.md)", text)
        self.assertIn("`docs/plan.md`: [bm-1]", text)

    def test_board_can_be_turned_off(self):
        self.svc.init()
        cfg = self.svc.dir.dir / "config.toml"
        cfg.write_text(cfg.read_text(encoding="utf-8") + 'board = ""\n',
                       encoding="utf-8")
        self.svc.dir._config = None
        self.svc.create("x")
        self.assertFalse((self.root / "TASKS.md").exists())
        self.assertIsNone(write_board(self.svc))


# ─── importer ────────────────────────────────────────────────────────


def _use(tid, name, inp, ts):
    return {"timestamp": ts, "sessionId": "4208dc59-aaaa",
            "message": {"content": [{"type": "tool_use", "id": tid,
                                     "name": name, "input": inp}]}}


def _result(tid, text, ts):
    return {"timestamp": ts, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": text}]}]}}


def _transcript(path: Path) -> Path:
    rows = [
        {"type": "user", "message": {"content": "unrelated line"}},
        _use("c1", "TaskCreate", {"subject": "Base", "description": "v1",
                                  "activeForm": "Basing"},
             "2026-05-01T07:00:00Z"),
        _result("c1", "Task #1 created successfully: Base",
                "2026-05-01T07:00:01Z"),
        _use("c2", "TaskCreate", {"subject": "Run", "description": "go"},
             "2026-05-01T07:01:00Z"),
        _result("c2", "Task #2 created successfully: Run",
                "2026-05-01T07:01:01Z"),
        _use("c3", "TaskCreate", {"subject": "Dropped", "description": ""},
             "2026-05-01T07:02:00Z"),
        _result("c3", "Task #3 created successfully: Dropped",
                "2026-05-01T07:02:01Z"),
        _use("c4", "TaskCreate", {"subject": "lost"}, "2026-05-01T07:03:00Z"),
        _use("u1", "TaskUpdate", {"taskId": "2", "addBlockedBy": ["1"]},
             "2026-05-01T08:00:00Z"),
        _use("u2", "TaskUpdate", {"taskId": "1", "status": "in_progress",
                                  "description": "v2: pivoted"},
             "2026-05-01T09:00:00Z"),
        _use("u3", "TaskUpdate", {"taskId": "1", "status": "completed",
                                  "subject": "Base (done)",
                                  "description": "v3: result 0.71"},
             "2026-05-01T10:00:00Z"),
        _use("u4", "TaskUpdate", {"taskId": "3", "status": "deleted"},
             "2026-05-01T10:30:00Z"),
        _use("u5", "TaskUpdate", {"taskId": "99", "status": "completed"},
             "2026-05-01T11:00:00Z"),
        _use("u6", "TaskUpdate", {"taskId": "2", "status": "in_progress"},
             "2026-05-01T11:30:00Z"),
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                    encoding="utf-8")
    return path


class ImporterTests(_Base):
    def setUp(self):
        super().setUp()
        self.src = _transcript(self.tmp / "4208dc59.jsonl")

    def test_replay(self):
        res = replay(self.src)
        self.assertEqual(sorted(res.tasks), [1, 2, 3])
        self.assertEqual(res.orphan_updates, 1)
        self.assertEqual(res.unnumbered_creates, 1)
        self.assertEqual(res.session, "4208dc59-aaaa")
        t1 = res.tasks[1]
        self.assertEqual((t1.status, t1.subject, t1.description),
                         ("done", "Base (done)", "v3: result 0.71"))
        self.assertEqual([d for _, d in t1.earlier], ["v1", "v2: pivoted"])
        self.assertEqual(res.tasks[2].blocked_by, [1])

    def test_dry_run_writes_nothing(self):
        buf = io.StringIO()
        self.assertEqual(run_import(self.svc, str(self.src), dry_run=True,
                                    out=buf), 0)
        self.assertIn("3 tasks", buf.getvalue())
        self.assertIn("bm-2 [active]", buf.getvalue())
        self.assertFalse(self.svc.dir.dir.exists())

    def test_import(self):
        buf = io.StringIO()
        run_import(self.svc, str(self.src), out=buf)
        d = self.svc.dir
        self.assertTrue((d.archive / "bm-1.md").is_file())
        self.assertTrue((d.archive / "bm-3.md").is_file())
        self.assertTrue((d.dir / "bm-2.md").is_file())
        t1 = Task.from_markdown((d.archive / "bm-1.md").read_text("utf-8"))
        self.assertEqual(t1.title, "Base (done)")
        self.assertEqual(t1.description, "v3: result 0.71")
        earlier = t1.section("Earlier descriptions")
        self.assertLess(earlier.index("v2: pivoted"), earlier.index("v1"))
        self.assertEqual(t1.created, "2026-05-01T07:00:00Z")
        self.assertEqual(t1.updated, "2026-05-01T10:00:00Z")
        self.assertIn("2026-05-01 10:00Z [4208dc59] active → done; retitled "
                      "(was: Base); description rewritten",
                      t1.log_lines[0])
        self.assertTrue(t1.log_lines[-1].endswith(
            "imported from Claude Code task #1"))
        self.assertEqual(t1.extra["source"], "claude-code 4208dc59 #1")
        t2 = Task.from_markdown((d.dir / "bm-2.md").read_text("utf-8"))
        self.assertEqual((t2.status, t2.depends), ("active", ["bm-1"]))
        self.assertEqual(self.index.counts("backup_models"),
                         {"done": 1, "active": 1, "cancelled": 1})
        self.assertIn("bm-2", (self.root / "TASKS.md").read_text("utf-8"))
        # New tasks continue the numbering.
        self.assertEqual(self.svc.create("next").id, "bm-4")

    def test_rerun_resumes(self):
        run_import(self.svc, str(self.src), out=io.StringIO())
        (self.svc.dir.dir / "bm-2.md").unlink()
        buf = io.StringIO()
        run_import(self.svc, str(self.src), out=buf)
        self.assertIn("2 already imported; 1 to go", buf.getvalue())
        self.assertTrue((self.svc.dir.dir / "bm-2.md").is_file())

    def test_embedded_heading_does_not_split_the_task(self):
        t = Task(id="bm-1", title="x")
        t.set_section("Description", "intro\n## Results\nnumbers")
        back = Task.from_markdown(t.to_markdown())
        self.assertEqual([h for h, _ in back.sections], ["Description"])
        self.assertIn("### Results", back.description)


class CliTests(_Base):
    with_index = False

    def run_cli(self, *argv):
        from claude_hooks.tasks.cli import main
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["-C", str(self.root), "--no-index", *argv])
        return rc, buf.getvalue()

    def test_add_ready_done(self):
        rc, out = self.run_cli("add", "Write docs", "-p", "H", "--accept",
                               "runbook")
        self.assertEqual(rc, 0, out)
        self.assertIn("bm-1 [pending H]", out)
        rc, out = self.run_cli("ready")
        self.assertIn("bm-1", out)
        rc, out = self.run_cli("done", "bm-1", "-m", "written")
        self.assertIn("[done H]", out)
        rc, out = self.run_cli("done", "bm-7")
        self.assertEqual(rc, 1)

    def test_init_with_prefix(self):
        rc, out = self.run_cli("init", "--prefix", "bk")
        self.assertIn("prefix bk", out)
        _, out = self.run_cli("add", "x")
        self.assertIn("bk-1", out)


if __name__ == "__main__":
    unittest.main()
