"""Tests for :mod:`consultants.engine.tool_executor` and the M6
state-channel additions.

Layers covered:

1. **State channels** — ``ToolPlanItem`` / ``ToolResult``
   dataclasses + the round-filtering helper.
2. **Prompt builder** ``build_tool_executor_messages`` — system
   prompt + intent surfacing + grounding ordering + optional
   ``why``/``suggested_tools`` blocks.
3. **Node behavior** — happy path returns a populated
   ``ToolResult``; missing plan item tombstones cleanly; loop
   exception tombstones with the error; recorder + event hooks
   fire when present; tools_called list captures the order of
   ``_on_tool`` callbacks.
4. **Channel reducer** — additive merge across multiple Send
   lanes preserves order, the round filter returns only the
   matching parent_round rows.

All tests use stub ``loop_runner`` / ``chat_client`` / ``recorder``
so they run on the main ``claude-hooks`` env without langgraph.
"""

from __future__ import annotations

import operator
import unittest

from consultants.engine.state_v2 import (
    ToolPlanItem,
    ToolResult,
    tool_results_for_round,
)
from consultants.engine.tool_executor import (
    TOOL_EXECUTOR_SYSTEM,
    build_tool_executor_messages,
    tool_executor_node,
    _extract_final_content,
    _summarize_tools,
)


# ============================================================== #
# Dataclasses
# ============================================================== #

class TestToolPlanItem(unittest.TestCase):

    def test_minimal_construction(self):
        item = ToolPlanItem(intent="find function X")
        self.assertEqual(item.intent, "find function X")
        self.assertEqual(item.why, "")
        self.assertIsNone(item.lane_idx)
        self.assertEqual(item.parent_round, 1)
        self.assertEqual(item.suggested_tools, [])

    def test_full_construction(self):
        item = ToolPlanItem(
            intent="audit auth",
            why="user asked about session security",
            lane_idx=3,
            parent_round=2,
            suggested_tools=["grep", "read_file"],
        )
        self.assertEqual(item.lane_idx, 3)
        self.assertEqual(item.parent_round, 2)
        self.assertEqual(item.suggested_tools, ["grep", "read_file"])

    def test_frozen(self):
        item = ToolPlanItem(intent="x")
        with self.assertRaises(Exception):
            item.intent = "y"  # type: ignore


class TestToolResult(unittest.TestCase):

    def test_minimal_construction(self):
        r = ToolResult(intent="x")
        self.assertEqual(r.intent, "x")
        self.assertEqual(r.content, "")
        self.assertEqual(r.tools_called, [])
        self.assertIsNone(r.error)

    def test_tombstone_shape(self):
        r = ToolResult(
            intent="audit", content="",
            error="RuntimeError: boom",
            lane_idx=2, parent_round=1,
        )
        self.assertEqual(r.error, "RuntimeError: boom")
        self.assertEqual(r.content, "")


# ============================================================== #
# tool_results_for_round
# ============================================================== #

class TestToolResultsForRound(unittest.TestCase):

    def test_filters_by_parent_round(self):
        state = {"tool_results": [
            ToolResult(intent="a", parent_round=1),
            ToolResult(intent="b", parent_round=1),
            ToolResult(intent="c", parent_round=2),
        ]}
        round1 = tool_results_for_round(state, 1)
        self.assertEqual(len(round1), 2)
        self.assertEqual([r.intent for r in round1], ["a", "b"])
        round2 = tool_results_for_round(state, 2)
        self.assertEqual(len(round2), 1)
        self.assertEqual(round2[0].intent, "c")

    def test_returns_empty_when_no_results(self):
        self.assertEqual(tool_results_for_round({}, 1), [])
        self.assertEqual(
            tool_results_for_round({"tool_results": []}, 1), [],
        )


# ============================================================== #
# build_tool_executor_messages
# ============================================================== #

class TestBuildToolExecutorMessages(unittest.TestCase):

    def test_basic_shape(self):
        item = ToolPlanItem(intent="find caller graph for X")
        msgs = build_tool_executor_messages(item, [])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], TOOL_EXECUTOR_SYSTEM)
        self.assertEqual(msgs[1]["role"], "user")
        self.assertIn("INTENT TO EXECUTE", msgs[1]["content"])
        self.assertIn("find caller graph for X", msgs[1]["content"])

    def test_grounding_prepended(self):
        grounding = [
            {"role": "system", "content": "anchor: project map"},
        ]
        item = ToolPlanItem(intent="x")
        msgs = build_tool_executor_messages(item, grounding)
        self.assertEqual(msgs[0]["content"], "anchor: project map")
        self.assertEqual(msgs[1]["content"], TOOL_EXECUTOR_SYSTEM)

    def test_why_block_surfaced(self):
        item = ToolPlanItem(
            intent="find X", why="researcher noticed Y",
        )
        msgs = build_tool_executor_messages(item, [])
        self.assertIn("WHY (researcher's reason)", msgs[1]["content"])
        self.assertIn("researcher noticed Y", msgs[1]["content"])

    def test_suggested_tools_block(self):
        item = ToolPlanItem(
            intent="search",
            suggested_tools=["grep", "glob"],
        )
        msgs = build_tool_executor_messages(item, [])
        self.assertIn("SUGGESTED TOOLS", msgs[1]["content"])
        self.assertIn("grep", msgs[1]["content"])
        self.assertIn("glob", msgs[1]["content"])

    def test_parent_question_surfaced_when_provided(self):
        item = ToolPlanItem(intent="x")
        msgs = build_tool_executor_messages(item, [], question="Q")
        self.assertIn("PARENT QUESTION", msgs[1]["content"])
        self.assertIn("Q", msgs[1]["content"])

    def test_omits_optional_blocks_when_empty(self):
        item = ToolPlanItem(intent="x")
        msgs = build_tool_executor_messages(item, [], question="")
        user = msgs[1]["content"]
        self.assertNotIn("PARENT QUESTION", user)
        self.assertNotIn("WHY", user)
        self.assertNotIn("SUGGESTED TOOLS", user)


# ============================================================== #
# tool_executor_node
# ============================================================== #

class _FakeChat:
    """ChatClient stub — never called when a stub loop_runner is
    provided, but the node binds it through ``_chat_fn``."""
    def __init__(self, reply="ok"):
        self.reply = reply

    def chat(self, payload: dict, *, think=False) -> dict:
        return {
            "choices": [{
                "message": {"role": "assistant", "content": self.reply},
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


def _stub_loop_runner_happy(reply_content: str = "DONE: evidence here"):
    """Build a stub run_loop returning the run_loop's typical shape."""
    def _runner(payload, cwd, *, config, tool_specs, chat_fn,
                tool_executor, on_iter=None, on_tool=None,
                preseed_builder=None):
        # Simulate one tool call so on_tool gets exercised — proves
        # the wiring works for the recorder path.
        if on_tool is not None:
            on_tool("read_file", '{"path":"x.py"}', "content", 12, None)
        if on_iter is not None:
            on_iter(0, payload, {"choices": [
                {"message": {"content": reply_content}},
            ]}, 100)
        return {"final": {"choices": [
            {"message": {"role": "assistant", "content": reply_content}},
        ]}}
    return _runner


def _stub_loop_runner_raises(exc: Exception):
    def _runner(payload, cwd, *, config, tool_specs, chat_fn,
                tool_executor, on_iter=None, on_tool=None,
                preseed_builder=None):
        raise exc
    return _runner


class _FakeRecorder:
    def __init__(self):
        self.nodes: list[dict] = []
        self.llms: list[dict] = []
        self.tools: list[dict] = []

    def record_node(self, **kw):
        self.nodes.append(kw)

    def record_llm(self, **kw):
        self.llms.append(kw)

    def record_tool(self, **kw):
        self.tools.append(kw)


class TestToolExecutorNodeHappyPath(unittest.TestCase):

    def test_returns_populated_tool_result(self):
        item = ToolPlanItem(
            intent="find X", why="audit", lane_idx=2,
            parent_round=1, suggested_tools=["grep"],
        )
        state = {
            "tool_plan_item": item,
            "lane_idx": 2,
            "question": "What is X?",
        }
        out = tool_executor_node(
            state,
            chat_client=_FakeChat(),
            tool_executor=lambda *a, **kw: "",
            tool_specs=[],
            grounding_msgs=[],
            model="gemma4:31b-cloud",
            cwd="/tmp",
            loop_runner=_stub_loop_runner_happy("EVIDENCE: x.py:42"),
        )
        results = out["tool_results"]
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.intent, "find X")
        self.assertEqual(r.content, "EVIDENCE: x.py:42")
        self.assertEqual(r.lane_idx, 2)
        self.assertEqual(r.parent_round, 1)
        self.assertIsNone(r.error)
        self.assertEqual(r.tools_called, ["read_file"])
        # transcript_summary collapses the tool_called list.
        self.assertIn("read_file", r.transcript_summary)
        # duration_ms is non-negative.
        self.assertGreaterEqual(r.duration_ms, 0)

    def test_recorder_callbacks_fire(self):
        rec = _FakeRecorder()
        item = ToolPlanItem(intent="x", parent_round=1)
        state = {"tool_plan_item": item, "lane_idx": 1}
        tool_executor_node(
            state,
            chat_client=_FakeChat(),
            tool_executor=lambda *a, **kw: "",
            tool_specs=[], grounding_msgs=[],
            model="m", cwd="/tmp", recorder=rec,
            loop_runner=_stub_loop_runner_happy("done"),
        )
        # node_enter + node_exit
        self.assertGreaterEqual(len(rec.nodes), 2)
        kinds = [n.get("kind") for n in rec.nodes]
        self.assertIn("node_enter", kinds)
        self.assertIn("node_exit", kinds)
        # llm + tool rows tagged with role="tool_executor"
        self.assertEqual(len(rec.llms), 1)
        self.assertEqual(rec.llms[0]["role"], "tool_executor")
        self.assertEqual(len(rec.tools), 1)
        self.assertEqual(rec.tools[0]["role"], "tool_executor")
        self.assertEqual(rec.tools[0]["tool"], "read_file")


class TestToolExecutorNodeTombstones(unittest.TestCase):

    def test_missing_plan_item_tombstones(self):
        # Defensive path: a Send without an item is a graph-wiring
        # bug; the node returns a tombstone rather than crashing.
        state = {"lane_idx": 0}
        out = tool_executor_node(
            state, chat_client=_FakeChat(),
            tool_executor=lambda *a, **kw: "",
            tool_specs=[], grounding_msgs=[],
            model="m", cwd="/tmp",
            loop_runner=_stub_loop_runner_happy("never called"),
        )
        r = out["tool_results"][0]
        self.assertEqual(r.intent, "(missing)")
        self.assertIsNotNone(r.error)
        self.assertIn("tool_plan_item", r.error)

    def test_empty_intent_tombstones(self):
        state = {"tool_plan_item": ToolPlanItem(intent="   ")}
        out = tool_executor_node(
            state, chat_client=_FakeChat(),
            tool_executor=lambda *a, **kw: "",
            tool_specs=[], grounding_msgs=[],
            model="m", cwd="/tmp",
            loop_runner=_stub_loop_runner_happy("x"),
        )
        self.assertIsNotNone(out["tool_results"][0].error)

    def test_loop_exception_tombstones(self):
        item = ToolPlanItem(intent="x", parent_round=2, lane_idx=3)
        state = {"tool_plan_item": item, "lane_idx": 3}
        out = tool_executor_node(
            state, chat_client=_FakeChat(),
            tool_executor=lambda *a, **kw: "",
            tool_specs=[], grounding_msgs=[],
            model="m", cwd="/tmp",
            loop_runner=_stub_loop_runner_raises(
                RuntimeError("upstream down"),
            ),
        )
        r = out["tool_results"][0]
        self.assertEqual(r.intent, "x")
        self.assertIn("RuntimeError", r.error)
        self.assertIn("upstream down", r.error)
        # parent_round + lane_idx propagated even on failure so the
        # researcher's filter at round 2 still surfaces the tombstone.
        self.assertEqual(r.parent_round, 2)
        self.assertEqual(r.lane_idx, 3)


# ============================================================== #
# Internal helpers
# ============================================================== #

class TestExtractFinalContent(unittest.TestCase):

    def test_run_loop_shape(self):
        result = {"final": {"choices": [
            {"message": {"content": "hello"}},
        ]}}
        self.assertEqual(_extract_final_content(result), "hello")

    def test_choices_at_top_level(self):
        result = {"choices": [
            {"message": {"content": "no final wrapper"}},
        ]}
        self.assertEqual(
            _extract_final_content(result), "no final wrapper",
        )

    def test_string_passthrough(self):
        self.assertEqual(_extract_final_content("raw"), "raw")

    def test_none_returns_empty(self):
        self.assertEqual(_extract_final_content(None), "")

    def test_unknown_shape_stringifies(self):
        self.assertIn("42", _extract_final_content(42))


class TestSummarizeTools(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(_summarize_tools([]), "(no tools called)")

    def test_unique_preserves_order(self):
        s = _summarize_tools(["grep", "read_file", "glob"])
        self.assertEqual(s, "grep ×1, read_file ×1, glob ×1")

    def test_collapses_duplicates_with_count(self):
        s = _summarize_tools(["read_file", "grep", "read_file"])
        # First occurrence dictates ordering; count is total.
        self.assertEqual(s, "read_file ×2, grep ×1")


# ============================================================== #
# Channel reducer sanity — additive merge across lanes
# ============================================================== #

class TestChannelReducer(unittest.TestCase):
    """``tool_results`` uses ``operator.add`` (list concat) so two
    parallel Sends merging cleanly is the contract."""

    def test_additive_concat(self):
        left = [ToolResult(intent="a", lane_idx=0)]
        right = [ToolResult(intent="b", lane_idx=1)]
        merged = operator.add(left, right)
        self.assertEqual(len(merged), 2)
        self.assertEqual([r.intent for r in merged], ["a", "b"])

    def test_three_way_merge_preserves_order(self):
        l1 = [ToolResult(intent="a")]
        l2 = [ToolResult(intent="b")]
        l3 = [ToolResult(intent="c")]
        merged = operator.add(operator.add(l1, l2), l3)
        self.assertEqual([r.intent for r in merged], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
