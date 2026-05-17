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
    RESEARCHER_PLAN_MODE_BLOCK,
    TOOL_EXECUTOR_SYSTEM,
    build_tool_executor_messages,
    build_tool_plan_user_appendix,
    parse_tool_plan,
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
        # #103: parent_lane_idx defaults to None — the single-
        # researcher / pre-refactor path. The fanback router
        # treats None lanes as "no per-researcher filter needed".
        self.assertIsNone(item.parent_lane_idx)
        self.assertEqual(item.parent_round, 1)
        self.assertEqual(item.suggested_tools, [])

    def test_full_construction(self):
        item = ToolPlanItem(
            intent="audit auth",
            why="user asked about session security",
            lane_idx=3,
            parent_lane_idx=5,
            parent_round=2,
            suggested_tools=["grep", "read_file"],
        )
        self.assertEqual(item.lane_idx, 3)
        # #103: parent_lane_idx records WHICH researcher lane
        # emitted this plan item — the globally-unique lane_idx
        # from Phase 9 multi-model researcher fanout.
        self.assertEqual(item.parent_lane_idx, 5)
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
        # #103: parent_lane_idx defaults to None on legacy /
        # single-researcher results.
        self.assertIsNone(r.parent_lane_idx)
        self.assertIsNone(r.error)

    def test_tombstone_shape(self):
        r = ToolResult(
            intent="audit", content="",
            error="RuntimeError: boom",
            lane_idx=2, parent_lane_idx=5, parent_round=1,
        )
        self.assertEqual(r.error, "RuntimeError: boom")
        self.assertEqual(r.content, "")
        # #103: parent_lane_idx round-trips even on tombstones
        # so the post-mortem can attribute failures to a specific
        # researcher lane.
        self.assertEqual(r.parent_lane_idx, 5)


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

    # ---- #103: parent_lane_idx filter overload ---------------------

    def test_parent_lane_filter_matches_own_lane(self):
        """Passing ``parent_lane_idx=N`` filters to results whose
        parent_lane_idx equals N — the #103 cross-pollution fix
        in action."""
        state = {"tool_results": [
            ToolResult(intent="a", parent_round=1, parent_lane_idx=5),
            ToolResult(intent="b", parent_round=1, parent_lane_idx=7),
            ToolResult(intent="c", parent_round=2, parent_lane_idx=5),
        ]}
        lane5_r1 = tool_results_for_round(state, 1, parent_lane_idx=5)
        self.assertEqual([r.intent for r in lane5_r1], ["a"])
        lane7_r1 = tool_results_for_round(state, 1, parent_lane_idx=7)
        self.assertEqual([r.intent for r in lane7_r1], ["b"])
        lane5_r2 = tool_results_for_round(state, 2, parent_lane_idx=5)
        self.assertEqual([r.intent for r in lane5_r2], ["c"])

    def test_parent_lane_none_returns_every_match_for_round(self):
        """Passing ``parent_lane_idx=None`` (the default) is the
        pre-#103 contract: return every result for the round
        regardless of which lane emitted it. Used by the single-
        researcher non-fanout path."""
        state = {"tool_results": [
            ToolResult(intent="a", parent_round=1, parent_lane_idx=5),
            ToolResult(intent="b", parent_round=1, parent_lane_idx=7),
            ToolResult(intent="c", parent_round=2, parent_lane_idx=5),
        ]}
        all_r1 = tool_results_for_round(state, 1)
        self.assertEqual([r.intent for r in all_r1], ["a", "b"])
        all_r1_explicit = tool_results_for_round(
            state, 1, parent_lane_idx=None,
        )
        self.assertEqual(
            [r.intent for r in all_r1_explicit], ["a", "b"],
        )

    def test_legacy_none_parent_lane_returns_to_every_filter(self):
        """A ToolResult with ``parent_lane_idx is None`` is the
        legacy / pre-#103 shape — it must surface to every lane
        filter rather than getting masked. Critical for the
        rollout: in-flight checkpoints with None values keep
        producing correct REPORT-mode prompts under the new
        engine wiring."""
        state = {"tool_results": [
            ToolResult(intent="own", parent_round=1, parent_lane_idx=5),
            ToolResult(
                intent="legacy", parent_round=1, parent_lane_idx=None,
            ),
            ToolResult(
                intent="sibling", parent_round=1, parent_lane_idx=7,
            ),
        ]}
        lane5 = tool_results_for_round(state, 1, parent_lane_idx=5)
        self.assertEqual([r.intent for r in lane5], ["own", "legacy"])
        # The sibling's stamped non-None value must be filtered out.
        self.assertNotIn(
            "sibling", [r.intent for r in lane5],
        )

    def test_parent_lane_filter_no_match_returns_empty(self):
        """A parent_lane_idx that doesn't match any result and
        no legacy-None rows present returns the empty list (not
        an exception)."""
        state = {"tool_results": [
            ToolResult(intent="x", parent_round=1, parent_lane_idx=5),
        ]}
        self.assertEqual(
            tool_results_for_round(state, 1, parent_lane_idx=99), [],
        )
        # Round mismatch with lane filter is also empty.
        self.assertEqual(
            tool_results_for_round(state, 2, parent_lane_idx=5), [],
        )

    def test_parent_lane_filter_preserves_emission_order(self):
        """Round-filter is order-preserving so REPORT-mode prompts
        render ToolResults in the order tool_executor returned
        them. Verifies the parent_lane_idx filter doesn't shuffle."""
        state = {"tool_results": [
            ToolResult(intent="first", parent_round=1, parent_lane_idx=5),
            ToolResult(intent="second", parent_round=1, parent_lane_idx=5),
            ToolResult(intent="third", parent_round=1, parent_lane_idx=5),
        ]}
        out = tool_results_for_round(state, 1, parent_lane_idx=5)
        self.assertEqual(
            [r.intent for r in out], ["first", "second", "third"],
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


# ============================================================== #
# M6b — parse_tool_plan
# ============================================================== #

class TestParseToolPlan(unittest.TestCase):

    def test_empty_input_returns_empty(self):
        self.assertEqual(parse_tool_plan(""), [])
        self.assertEqual(parse_tool_plan(None), [])  # type: ignore
        self.assertEqual(parse_tool_plan("   \n  "), [])

    def test_fenced_json_with_tool_plan_key(self):
        text = (
            'Plan:\n```json\n{"tool_plan": ['
            '{"intent": "find login()", "why": "audit"},'
            '{"intent": "check sessions"}'
            ']}\n```'
        )
        items = parse_tool_plan(text, parent_round=3)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].intent, "find login()")
        self.assertEqual(items[0].why, "audit")
        self.assertEqual(items[0].parent_round, 3)
        self.assertEqual(items[0].lane_idx, 0)
        self.assertEqual(items[1].intent, "check sessions")
        self.assertEqual(items[1].lane_idx, 1)

    def test_fenced_json_without_json_tag(self):
        # Some models fence with bare ``` not ```json.
        text = '```\n{"tool_plan": [{"intent": "x"}]}\n```'
        items = parse_tool_plan(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].intent, "x")

    def test_bare_json_object(self):
        text = '{"tool_plan": [{"intent": "bare"}]}'
        items = parse_tool_plan(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].intent, "bare")

    def test_bare_json_array(self):
        # Sometimes the model omits the wrapper and emits the list.
        text = '[{"intent": "first"}, {"intent": "second"}]'
        items = parse_tool_plan(text)
        self.assertEqual(len(items), 2)
        self.assertEqual([i.intent for i in items], ["first", "second"])

    def test_unparseable_text_returns_empty(self):
        # Non-JSON narrative — the M6 PLAN-mode fallback path
        # depends on this returning empty.
        self.assertEqual(
            parse_tool_plan("Just a research summary, no plan."),
            [],
        )

    def test_malformed_json_returns_empty(self):
        self.assertEqual(parse_tool_plan('{"tool_plan": [malformed'), [])

    def test_items_missing_intent_skipped(self):
        text = '{"tool_plan": [{"why": "no intent"}, {"intent": "ok"}]}'
        items = parse_tool_plan(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].intent, "ok")

    def test_suggested_tools_optional(self):
        text = ('{"tool_plan": [{"intent": "x", '
                '"suggested_tools": ["grep", "read_file"]}]}')
        items = parse_tool_plan(text)
        self.assertEqual(items[0].suggested_tools,
                         ["grep", "read_file"])

    def test_suggested_tools_filters_non_strings(self):
        text = ('{"tool_plan": [{"intent": "x", '
                '"suggested_tools": ["grep", 42, null, "glob"]}]}')
        items = parse_tool_plan(text)
        self.assertEqual(items[0].suggested_tools, ["grep", "glob"])

    def test_empty_intent_skipped(self):
        text = '{"tool_plan": [{"intent": "   "}, {"intent": "real"}]}'
        items = parse_tool_plan(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].intent, "real")

    def test_multiple_fences_prefers_first_with_valid_plan(self):
        text = (
            'first: ```{"foo": "bar"}```\n'
            'second: ```json\n{"tool_plan": [{"intent": "use this"}]}\n```'
        )
        items = parse_tool_plan(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].intent, "use this")


# ============================================================== #
# M6b — PLAN-mode prompt fragment + REPORT-mode appendix
# ============================================================== #

class TestPlanModeBlock(unittest.TestCase):

    def test_contains_required_anchors(self):
        # The PLAN-mode block must declare DO-NOT-CALL-TOOLS,
        # name the output shape (JSON tool_plan), and bound the
        # item count.
        b = RESEARCHER_PLAN_MODE_BLOCK
        self.assertIn("DO NOT CALL TOOLS", b)
        self.assertIn("tool_plan", b)
        self.assertIn("```json", b)
        self.assertIn("1-6 items", b)


class TestBuildToolPlanUserAppendix(unittest.TestCase):

    def test_empty_returns_empty_string(self):
        self.assertEqual(build_tool_plan_user_appendix([]), "")

    def test_renders_single_result(self):
        r = ToolResult(
            intent="find login()", content="auth.py:42 has it",
            transcript_summary="read_file ×2",
        )
        out = build_tool_plan_user_appendix([r])
        self.assertIn("PRIOR TOOL RESULTS", out)
        self.assertIn("find login()", out)
        self.assertIn("auth.py:42 has it", out)
        self.assertIn("read_file ×2", out)

    def test_renders_tombstone_compactly(self):
        r = ToolResult(
            intent="x", content="",
            error="RuntimeError: upstream down",
        )
        out = build_tool_plan_user_appendix([r])
        self.assertIn("FAILED", out)
        self.assertIn("upstream down", out)

    def test_truncates_long_content(self):
        big = "Z" * 3000
        r = ToolResult(intent="x", content=big)
        out = build_tool_plan_user_appendix([r])
        self.assertIn("truncated", out)
        # Content body is capped — total length is bounded.
        self.assertLess(len(out), 3000)

    def test_multiple_results_numbered(self):
        a = ToolResult(intent="a", content="found A")
        b = ToolResult(intent="b", content="found B")
        out = build_tool_plan_user_appendix([a, b])
        # Items numbered 1, 2 — order preserved.
        idx1 = out.index("1.")
        idx2 = out.index("2.")
        self.assertLess(idx1, idx2)


if __name__ == "__main__":
    unittest.main()
