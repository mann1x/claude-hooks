"""The task graph across projects (ch-28 phase 1, docs/PLAN-task-graph.md).

Two projects share one index, as every project on a host does. A task
writes only its own project's files; edges into the other project are
stored on our side and resolved through the index. An id that cannot be
resolved is blocking, never done.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claude_hooks.tasks.graph import Graph, Ref
from claude_hooks.tasks.service import TaskService
from claude_hooks.tasks.store import TaskIndex
from claude_hooks.tasks.tools import TaskTools
from tests.test_tasks import _SqliteConn


class _BrokenIndex:
    """An index whose cross-project reads fail, as when Postgres is down."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in ("projects", "edges", "project_row", "max_number"):
            def fail(*a, **k):
                raise OSError("connection refused")
            return fail
        return getattr(self._inner, name)


class _Two(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        db = _SqliteConn(self.tmp / "t.db")
        self.addCleanup(db.close)
        self.index = TaskIndex(db, db.lock, dialect="sqlite")
        self.ch = self.make("claude-hooks", "ch")
        self.op = self.make("opencoti", "op")
        for i in range(3):
            self.op.create(f"opencoti task {i + 1}")

    def make(self, name: str, prefix: str, index="shared") -> TaskService:
        root = self.tmp / name
        root.mkdir(exist_ok=True)
        s = TaskService(root, self.index if index == "shared" else index,
                        host="solidpc", session_id="s1")
        s.init(prefix=prefix, project=name)
        return s

    def board_ids(self, svc, key):
        return [r["id"] for r in svc.board()[key]]


class CrossProjectDependsTests(_Two):
    def test_a_task_waits_for_another_projects_task(self):
        t = self.ch.create("ship after opencoti fixes it", depends=["op-2"])
        self.assertEqual(t.depends, ["op-2"])
        row = self.ch.board()["blocked"][0]
        self.assertEqual((row["id"], row["open_depends"]), (t.id, ["op-2"]))
        self.op.set_status("op-2", "done")
        self.assertEqual(self.board_ids(self.ch, "ready"), [t.id])

    def test_a_cancelled_dependency_unblocks_too(self):
        t = self.ch.create("x", depends=["OP-1"])
        self.assertEqual(t.depends, ["op-1"])
        self.op.set_status("op-1", "cancelled")
        self.assertEqual(self.board_ids(self.ch, "ready"), [t.id])

    def test_unknown_project_and_unknown_task_are_refused(self):
        with self.assertRaises(ValueError) as cm:
            self.ch.create("typo", depends=["zz-1"])
        self.assertIn("no project uses the prefix 'zz'", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            self.ch.create("typo", depends=["op-99"])
        self.assertIn("no task op-99 in opencoti", str(cm.exception))
        with self.assertRaises(ValueError):
            self.ch.create("local typo", depends=["ch-99"])

    def test_with_the_index_down_it_is_kept_and_stays_blocking(self):
        down = TaskService(self.ch.dir.root, _BrokenIndex(self.index),
                           host="solidpc")
        t = down.create("written while postgres is down", depends=["op-1"])
        self.assertEqual(t.depends, ["op-1"])
        self.op.set_status("op-1", "done")
        blocked = down.board()["blocked"]
        self.assertEqual([r["id"] for r in blocked], [t.id])
        self.assertIn("unavailable", blocked[0]["unresolved"]["op-1"])
        # Once the index answers again, the done dependency counts.
        self.assertEqual(self.board_ids(self.ch, "ready"), [t.id])

    def test_a_host_without_an_index_keeps_foreign_ids_blocking(self):
        alone = TaskService(self.ch.dir.root, None, host="solidpc")
        t = alone.create("files only", depends=["op-1"])
        self.op.set_status("op-1", "done")
        self.assertEqual([r["id"] for r in alone.board()["blocked"]], [t.id])

    def test_another_projects_dependents_count_as_blocking(self):
        self.ch.create("needs op-1", depends=["op-1"])
        board = {r["id"]: r for r in self.op.board()["ready"]}
        self.assertGreater(board["op-1"]["urgency"], board["op-2"]["urgency"])
        self.assertEqual(board["op-1"]["blocking"], 1)


class CycleTests(_Two):
    def test_a_cycle_across_projects_is_refused_with_its_path(self):
        self.ch.create("needs op-1", depends=["op-1"])
        with self.assertRaises(ValueError) as cm:
            self.op.update("op-1", add_depends=["ch-1"])
        self.assertIn("op-1 → ch-1 → op-1", str(cm.exception))
        self.assertEqual(self.op.show("op-1")[0].depends, [])

    def test_a_local_cycle_is_refused(self):
        self.ch.create("a")
        self.ch.create("b", depends=["ch-1"])
        self.ch.create("c", depends=["ch-2"])
        with self.assertRaises(ValueError) as cm:
            self.ch.update("ch-1", depends=["ch-3"])
        self.assertIn("ch-1 → ch-3 → ch-2 → ch-1", str(cm.exception))

    def test_a_parent_chain_cannot_loop(self):
        self.ch.create("epic")
        self.ch.create("child")
        self.ch.link("ch-2", "parent", "ch-1")
        with self.assertRaises(ValueError):
            self.ch.link("ch-1", "parent", "ch-2")
        with self.assertRaises(ValueError):
            self.ch.link("ch-1", "parent", "ch-1")


class TypedLinkTests(_Two):
    def tools(self, svc):
        return TaskTools(lambda: svc, background_embed=False)

    def test_parent_has_one_value_and_shows_children(self):
        self.ch.create("epic")
        self.ch.create("other epic")
        self.ch.create("child")
        self.ch.link("ch-3", "parent", "ch-1")
        self.ch.link("ch-3", "epic", "ch-2")          # replaces
        self.assertEqual(self.ch.show("ch-3")[0].links["parent"], ["ch-2"])
        out = self.tools(self.ch).call("task-show", {"id": "ch-2"})
        self.assertIn("children: ch-3", out)

    def test_related_shows_from_both_sides_without_writing_the_other(self):
        self.ch.create("ours")
        before = (self.op.dir.dir / "op-1.md").read_text()
        self.ch.link("ch-1", "related", "op-1")
        self.assertEqual((self.op.dir.dir / "op-1.md").read_text(), before)
        out = self.tools(self.op).call("task-show", {"id": "op-1"})
        self.assertIn("related: ch-1 (claude-hooks, pending)", out)
        out = self.tools(self.ch).call("task-show", {"id": "ch-1"})
        self.assertIn("related: op-1 (opencoti, pending)", out)

    def test_discovered_from_and_caused_by_read_back(self):
        self.ch.create("found while working on op-3")
        self.ch.link("ch-1", "discovered-from", "op-3")
        self.ch.link("ch-1", "caused-by", "op-2")
        out = self.tools(self.op).call("task-show", {"id": "op-3"})
        self.assertIn("led to: ch-1", out)
        out = self.tools(self.op).call("task-show", {"id": "op-2"})
        self.assertIn("caused: ch-1", out)

    def test_supersedes_closes_a_local_task_and_only_records_a_foreign_one(self):
        self.ch.create("old plan")
        self.ch.create("new plan")
        self.ch.link("ch-2", "supersedes", "ch-1")
        old = self.ch.show("ch-1")[0]
        self.assertEqual(old.status, "cancelled")
        self.assertIn("superseded by ch-2", old.log_lines[0])
        self.ch.link("ch-2", "supersedes", "op-1")
        self.assertEqual(self.op.show("op-1")[0].status, "pending")
        self.assertIn("op-1", self.ch.show("ch-2")[0].links["supersedes"])

    def test_duplicate_of_closes_the_duplicate(self):
        self.ch.create("same bug as op-1")
        self.ch.link("ch-1", "duplicate-of", "op-1")
        t = self.ch.show("ch-1")[0]
        self.assertEqual(t.status, "cancelled")
        self.assertIn("duplicate of op-1", t.log_lines[0])

    def test_a_task_link_must_resolve(self):
        self.ch.create("x")
        with self.assertRaises(ValueError):
            self.ch.link("ch-1", "related", "zz-4")
        with self.assertRaises(ValueError):
            self.ch.link("ch-1", "related", "not an id")

    def test_show_lists_dependents_in_other_projects(self):
        self.ch.create("waits", depends=["op-2"])
        out = self.tools(self.op).call("task-show", {"id": "op-2"})
        self.assertIn("depended on by: ch-1 (claude-hooks, pending)", out)
        out = self.tools(self.ch).call("task-show", {"id": "ch-1"})
        self.assertIn("depends: op-2 (opencoti, pending)", out)


class GraphUnitTests(unittest.TestCase):
    def test_unblocked_by_needs_the_last_open_dependency(self):
        g = Graph("claude-hooks", "ch", [
            {"id": "ch-1", "status": "pending", "depends": ["op-1"]},
            {"id": "ch-2", "status": "pending", "depends": ["op-1", "ch-3"]},
            {"id": "ch-3", "status": "pending"},
            {"id": "ch-4", "status": "done", "depends": ["op-1"]},
            {"id": "op-1", "status": "active"},
        ])
        # ch-2 still waits for ch-3; ch-4 is closed.
        self.assertEqual([n.id for n in g.unblocked_by("op-1")], ["ch-1"])

    def test_ref_reasons(self):
        g = Graph("ch", "ch", [], None)
        self.assertEqual(g.ref("op-1").reason, "no task index on this host")
        self.assertTrue(Ref("op-1", reason="x").open)
        self.assertFalse(Ref("op-1", "opencoti", "done").open)


if __name__ == "__main__":
    unittest.main()
