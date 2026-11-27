"""Views, claims and defer (ch-28 phase 4, docs/PLAN-task-graph.md).

``task-graph`` draws the dependency graph in layers (layer 0 can start
now; one layer can run in parallel), ``view: blocked`` says what blocks
each blocked task, ``task-start`` claims a task for the session and
refuses one another live session holds, and ``defer`` hides a task and
its children from ``task-ready`` until a date.
"""
from __future__ import annotations

import unittest

from claude_hooks.tasks.graph import Graph, layers, mermaid, walk
from claude_hooks.tasks.model import Task
from claude_hooks.tasks.tools import TaskTools
from tests.test_tasks_graph import _Two
from tests.test_tasks_unblock import _Mailer, _Session


def _g():
    return Graph("claude-hooks", "ch", [
        {"id": "ch-1", "status": "pending", "title": "design"},
        {"id": "ch-2", "status": "pending", "title": "build",
         "depends": ["ch-1"]},
        {"id": "ch-3", "status": "pending", "title": "test",
         "depends": ["ch-2", "op-1"]},
        {"id": "ch-4", "status": "pending", "title": "docs"},
        {"id": "ch-5", "status": "done", "title": "old"},
        {"id": "op-1", "status": "active", "title": "upstream fix"},
    ])


class LayerTests(unittest.TestCase):
    def test_layers_order_the_work(self):
        g = _g()
        ids = ["ch-1", "ch-2", "ch-3", "ch-4", "ch-5", "op-1"]
        self.assertEqual(layers(g, ids),
                         [["ch-1", "ch-4", "op-1"], ["ch-2"], ["ch-3"]])

    def test_an_open_dependency_outside_the_set_keeps_it_off_layer_0(self):
        self.assertEqual(layers(_g(), ["ch-3"]), [[], ["ch-3"]])
        lv = layers(_g(), ["ch-3", "ch-4"])
        self.assertEqual(lv, [["ch-4"], ["ch-3"]])

    def test_walk_up_and_down(self):
        g = _g()
        self.assertEqual(walk(g, "ch-2", "down"), {"ch-2", "ch-1"})
        self.assertEqual(walk(g, "ch-2", "up"), {"ch-2", "ch-3"})
        self.assertEqual(walk(g, "op-1", "up"), {"op-1", "ch-3"})

    def test_mermaid_draws_dependency_arrows_by_project(self):
        out = mermaid(_g(), ["ch-1", "ch-2", "ch-3", "op-1"])
        self.assertTrue(out.startswith("flowchart TD"))
        self.assertIn("ch_1 --> ch_2", out)
        self.assertIn("op_1 --> ch_3", out)
        self.assertIn("subgraph claude-hooks", out)

    def test_a_hand_written_cycle_does_not_loop(self):
        g = Graph("p", "p", [
            {"id": "p-1", "status": "pending", "depends": ["p-2"]},
            {"id": "p-2", "status": "pending", "depends": ["p-1"]},
        ])
        self.assertEqual(sorted(sum(layers(g, ["p-1", "p-2"]), [])),
                         ["p-1", "p-2"])


class GraphToolTests(_Two):
    def setUp(self):
        super().setUp()
        self.tools = TaskTools(lambda: self.ch, background_embed=False)
        self.ch.create("design")
        self.ch.create("build", depends=["ch-1", "op-2"])

    def test_project_view_includes_other_projects_dependencies(self):
        out = self.tools.call("task-graph", {})
        self.assertIn("Layer 0 — can start now", out)
        self.assertIn("op-2 (opencoti) [pending]", out)
        self.assertIn("ch-2 [pending] · build ← ch-1, op-2", out)

    def test_up_from_another_projects_task(self):
        tools = TaskTools(lambda: self.op, background_embed=False)
        out = tools.call("task-graph", {"id": "op-2", "direction": "up"})
        self.assertIn("ch-2 (claude-hooks)", out)

    def test_blocked_view_names_dependencies_and_gates(self):
        self.ch.gate("ch-2", "add", "human:go")
        out = self.tools.call("task-graph", {"view": "blocked"})
        self.assertIn("ch-2 · build", out)
        self.assertIn("waits for ch-1 (pending)", out)
        self.assertIn("waits for op-2 (opencoti, pending)", out)
        self.assertIn("gate human:go", out)

    def test_mermaid_format(self):
        out = self.tools.call("task-graph", {"format": "mermaid"})
        self.assertIn("ch_1 --> ch_2", out)


class ClaimTests(_Two):
    def tools_as(self, alias, sessions):
        t = TaskTools(lambda: self.ch, background_embed=False)
        t.mailer_fn = lambda: _Mailer(sessions, alias=alias, host="solidpc")
        return t

    def test_start_claims_and_a_live_holder_refuses_others(self):
        self.ch.create("x")
        live = [_Session("claude-hooks", "solidpc", "/x")]
        out = self.tools_as("claude-hooks", live).call("task-start",
                                                       {"id": "ch-1"})
        self.assertIn("by claude-hooks@solidpc", out)
        self.assertEqual(self.ch.show("ch-1")[0].claimed_by,
                         "claude-hooks@solidpc")
        other = self.tools_as("claude-hooks-2", live)
        out = other.call("task-start", {"id": "ch-1"})
        self.assertTrue(out.startswith("Not done: ch-1 is claimed by "
                                       "claude-hooks@solidpc"))
        out = other.call("task-start", {"id": "ch-1", "force": True})
        self.assertIn("by claude-hooks-2@solidpc", out)
        self.assertIn("taken over from claude-hooks@solidpc",
                      self.ch.show("ch-1")[0].log_lines[0])

    def test_a_holder_that_is_gone_does_not_block(self):
        self.ch.create("x")
        self.tools_as("old", []).call("task-start", {"id": "ch-1"})
        out = self.tools_as("new", []).call("task-start", {"id": "ch-1"})
        self.assertIn("by new@solidpc", out)

    def test_closing_clears_the_claim_and_the_cli_does_not_claim(self):
        self.ch.create("x")
        self.tools_as("a", []).call("task-start", {"id": "ch-1"})
        self.ch.set_status("ch-1", "done")
        self.assertEqual(self.ch.show("ch-1")[0].claimed_by, "")
        self.ch.create("y")
        TaskTools(lambda: self.ch, background_embed=False).call(
            "task-start", {"id": "ch-2"})
        self.assertEqual(self.ch.show("ch-2")[0].claimed_by, "")

    def test_a_hand_written_owner_key_stays_a_plain_extra(self):
        t = Task.from_markdown("---\nid: ch-9\ntitle: t\nstatus: pending\n"
                               "owner: manni\n---\n")
        self.assertEqual((t.extra, t.claimed_by), ({"owner": "manni"}, ""))

    def test_the_claim_reaches_the_index_row(self):
        self.ch.create("x")
        self.tools_as("a", []).call("task-start", {"id": "ch-1"})
        row = self.index.get("claude-hooks", "ch-1")
        self.assertEqual(row["claimed_by"], "a@solidpc")
        self.assertNotIn("_claimed_by", row["links"])


class DeferTests(_Two):
    def test_defer_hides_a_task_and_its_children_until_the_date(self):
        self.ch.create("epic")
        self.ch.create("child")
        self.ch.link("ch-2", "parent", "ch-1")
        self.ch.create("other")
        self.ch.update("ch-1", defer="2999-01-01")
        b = self.ch.board()
        self.assertEqual(sorted(r["id"] for r in b["deferred"]),
                         ["ch-1", "ch-2"])
        self.assertEqual([r["id"] for r in b["ready"]], ["ch-3"])
        self.assertEqual(self.index.get("claude-hooks", "ch-1")["defer"][:10],
                         "2999-01-01")
        self.ch.update("ch-1", defer="2020-01-01")       # date passed
        self.assertEqual(self.ch.board()["deferred"], [])
        self.ch.update("ch-1", defer="")
        self.assertEqual(self.ch.show("ch-1")[0].defer, "")
        self.assertIn("no longer deferred",
                      self.ch.show("ch-1")[0].log_lines[0])

    def test_an_active_task_is_never_deferred(self):
        self.ch.create("x", status="active")
        self.ch.update("ch-1", defer="2999-01-01")
        self.assertEqual([r["id"] for r in self.ch.board()["active"]],
                         ["ch-1"])


if __name__ == "__main__":
    unittest.main()
