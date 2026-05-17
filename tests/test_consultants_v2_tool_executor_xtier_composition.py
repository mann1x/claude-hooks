"""#103 — tool_executor + x-tier proper composition tests.

Tests the M11c-3 engine refactor: under Phase 9 multi-model
researcher fanout (xmedium / xhigh / xmax / xauto effort), each
researcher lane must compose cleanly with tool_executor without
cross-pollinating results from sibling lanes. The refactor:

1. Adds ``parent_lane_idx`` to :class:`ToolPlanItem` and
   :class:`ToolResult` so every plan item and every executor
   result records WHICH researcher lane emitted it.
2. Widens :func:`tool_results_for_round` with an optional
   ``parent_lane_idx`` filter so REPORT-mode prompts only see
   their own lane's results.
3. Drops the M6-era scalar ``awaiting_tool_results`` flag from
   :class:`CouncilStateV2`; the router now derives the dispatch
   decision from ``tool_plan`` vs ``tool_results`` at the
   current round (strictly more robust than last-writer-wins
   under N×M parallel writes).
4. Replaces the unconditional ``tool_executor → researcher``
   edge with a conditional ``_fanout_after_tool_executor`` that
   emits one ``Send`` per distinct ``parent_lane_idx`` for the
   current round. Single-researcher path returns the string
   ``"researcher"`` so base-tier behavior is bit-for-bit
   identical to the M6 wiring.

The headline test is :meth:`TestNoCrossPollution.\
test_two_researcher_lanes_three_items_each_no_pollution` —
6 ToolResults across two researcher lanes, each lane sees only
its own 3 in REPORT mode.
"""
from __future__ import annotations

import unittest

import pytest

from consultants.engine.state_v2 import (
    ToolPlanItem,
    ToolResult,
    tool_results_for_round,
)
from consultants.engine.tool_executor import (
    parse_tool_plan,
    tool_executor_node,
)


# Stand-alone import of the parity helpers — the test runs only
# in environments that have langgraph available (consultants env).
pytest_plugins: tuple = ()


def _has_langgraph() -> bool:
    try:
        import langgraph  # noqa: F401
        return True
    except Exception:
        return False


HAS_LANGGRAPH = _has_langgraph()


# ====================================================================== #
# 1. Data plumbing — parent_lane_idx round-trips through the stack
# ====================================================================== #


class TestParentLaneIdxRoundTrip(unittest.TestCase):
    """The end-to-end data plumbing: ``parent_lane_idx`` set by
    the researcher's :func:`parse_tool_plan` call must survive
    through the Send slice and land on the resulting ToolResult.
    """

    def test_parse_tool_plan_stamps_parent_lane_idx(self):
        """The researcher_node passes its own ``state["lane_idx"]``
        as ``parent_lane_idx``. ``parse_tool_plan`` stamps every
        emitted item with that value."""
        text = (
            '{"tool_plan": ['
            '{"intent": "find auth"},'
            '{"intent": "find session"}'
            ']}'
        )
        items = parse_tool_plan(
            text, parent_round=1, parent_lane_idx=5,
        )
        self.assertEqual(len(items), 2)
        for it in items:
            self.assertEqual(it.parent_lane_idx, 5)
            self.assertEqual(it.parent_round, 1)
        # The per-item lane_idx (position within the plan) is
        # independent of parent_lane_idx (which researcher emitted).
        self.assertEqual(items[0].lane_idx, 0)
        self.assertEqual(items[1].lane_idx, 1)

    def test_parse_tool_plan_default_parent_is_none(self):
        """Single-researcher path: ``parse_tool_plan`` without
        ``parent_lane_idx`` stamps ``None`` — pre-#103 shape, the
        single-researcher non-fanout default."""
        items = parse_tool_plan(
            '{"tool_plan": [{"intent": "x"}]}', parent_round=1,
        )
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].parent_lane_idx)

    def test_tool_executor_node_stamps_parent_from_state_slice(self):
        """The Send slice carries ``parent_lane_idx``;
        ``tool_executor_node`` reads it from state and stamps it
        on the emitted ToolResult — completing the data round-
        trip the router relies on."""

        def stub_loop(payload, cwd, **kw):
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "FINAL: result for lane 7",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 10},
            }

        class _FakeChat:
            def chat(self, payload, **_kw):
                return stub_loop(payload, None)

        item = ToolPlanItem(
            intent="find session handler",
            lane_idx=2,
            parent_lane_idx=7,
            parent_round=1,
        )
        state = {
            "tool_plan_item": item,
            "lane_idx": 2,
            "parent_lane_idx": 7,
            "question": "what handles sessions?",
        }
        out = tool_executor_node(
            state,
            chat_client=_FakeChat(),
            tool_executor=lambda *a, **kw: "",
            tool_specs=[],
            grounding_msgs=[],
            model="stub:test",
            cwd="/tmp",
            loop_runner=stub_loop,
        )
        self.assertEqual(len(out["tool_results"]), 1)
        result = out["tool_results"][0]
        # The parent_lane_idx must round-trip from the Send slice
        # onto the emitted ToolResult — this is the #103 invariant.
        self.assertEqual(result.parent_lane_idx, 7)
        # Companion fields preserved as well.
        self.assertEqual(result.lane_idx, 2)
        self.assertEqual(result.parent_round, 1)
        self.assertEqual(result.intent, "find session handler")

    def test_tool_executor_node_legacy_single_researcher_path(self):
        """Single-researcher path: state has no parent_lane_idx
        key (or it's None). The emitted ToolResult carries None,
        preserving the pre-#103 / base-tier shape."""

        def stub_loop(payload, cwd, **kw):
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "result",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        class _FakeChat:
            def chat(self, payload, **_kw):
                return stub_loop(payload, None)

        item = ToolPlanItem(intent="x", parent_round=1)
        state = {"tool_plan_item": item, "lane_idx": 0}
        out = tool_executor_node(
            state,
            chat_client=_FakeChat(),
            tool_executor=lambda *a, **kw: "",
            tool_specs=[],
            grounding_msgs=[],
            model="stub:test",
            cwd="/tmp",
            loop_runner=stub_loop,
        )
        result = out["tool_results"][0]
        self.assertIsNone(result.parent_lane_idx)


# ====================================================================== #
# 2. No cross-pollution at the data layer (the headline contract)
# ====================================================================== #


class TestNoCrossPollution(unittest.TestCase):
    """The contract M11c-3 must hold: at x-tier, no researcher
    lane sees a sibling lane's ToolResults in REPORT mode.
    Verified at the data-filter layer (where the bug previously
    manifested).
    """

    def test_two_researcher_lanes_three_items_each_no_pollution(self):
        """Two researcher lanes (parent_lane_idx=5 and =7) each
        emitted 3 plan items at round 1. Tool executor ran all 6
        and produced 6 ToolResults stamped with the right parent.
        At REPORT mode, lane 5's filter returns 3 lane-5 results
        and lane 7's filter returns 3 lane-7 results — zero
        overlap, zero cross-pollination."""
        results = [
            # Lane 5's results.
            ToolResult(intent="L5-find-auth",
                       parent_round=1, parent_lane_idx=5),
            ToolResult(intent="L5-find-session",
                       parent_round=1, parent_lane_idx=5),
            ToolResult(intent="L5-find-token",
                       parent_round=1, parent_lane_idx=5),
            # Lane 7's results.
            ToolResult(intent="L7-list-routes",
                       parent_round=1, parent_lane_idx=7),
            ToolResult(intent="L7-grep-middleware",
                       parent_round=1, parent_lane_idx=7),
            ToolResult(intent="L7-read-config",
                       parent_round=1, parent_lane_idx=7),
        ]
        state = {"tool_results": results}

        lane5_view = tool_results_for_round(
            state, 1, parent_lane_idx=5,
        )
        lane7_view = tool_results_for_round(
            state, 1, parent_lane_idx=7,
        )
        # Each lane sees exactly 3 items.
        self.assertEqual(len(lane5_view), 3)
        self.assertEqual(len(lane7_view), 3)
        # All of lane 5's intents start with "L5-"; none with "L7-".
        for r in lane5_view:
            self.assertTrue(
                r.intent.startswith("L5-"),
                f"lane 5 leaked {r.intent!r}",
            )
        for r in lane7_view:
            self.assertTrue(
                r.intent.startswith("L7-"),
                f"lane 7 leaked {r.intent!r}",
            )

    def test_unfiltered_call_still_sees_everything(self):
        """The single-researcher non-fanout path (no
        ``parent_lane_idx`` filter) still returns every result —
        preserves base-tier M12 parity behavior."""
        results = [
            ToolResult(intent="any-1", parent_round=1, parent_lane_idx=5),
            ToolResult(intent="any-2", parent_round=1, parent_lane_idx=7),
        ]
        state = {"tool_results": results}
        out = tool_results_for_round(state, 1)
        self.assertEqual(len(out), 2)

    def test_critic_reroute_round_2_sees_round_2_only(self):
        """Round-2 (critic reroute) per-lane filter: lane 5's
        REPORT-mode at round 2 sees only round-2 lane-5 results,
        not the round-1 cross-pollination."""
        results = [
            ToolResult(intent="r1-L5", parent_round=1, parent_lane_idx=5),
            ToolResult(intent="r1-L7", parent_round=1, parent_lane_idx=7),
            ToolResult(intent="r2-L5", parent_round=2, parent_lane_idx=5),
            ToolResult(intent="r2-L7", parent_round=2, parent_lane_idx=7),
        ]
        state = {"tool_results": results}
        out = tool_results_for_round(state, 2, parent_lane_idx=5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].intent, "r2-L5")


# ====================================================================== #
# 3. Routing topology — the conditional fanback replaces the old edge
# ====================================================================== #


@unittest.skipUnless(
    HAS_LANGGRAPH, "langgraph not installed in this env",
)
class TestRoutingTopology(unittest.TestCase):
    """The #103 refactor replaces the unconditional
    ``tool_executor → researcher`` edge with a conditional
    ``_fanout_after_tool_executor`` router. Inspect the compiled
    graph's branch registry to confirm the new shape lands.
    """

    def _build(self, *, with_extras: bool = True):
        """Build a compiled graph with tool_executor + researcher
        enabled. ``with_extras=True`` populates the researcher
        extras list so the multi-model fanback path can fire."""
        from tests._parity_helpers import build_default_deps
        from consultants.engine.graph import build_council_graph
        from langgraph.checkpoint.memory import InMemorySaver

        extras = (
            {"researcher": ["extra-1:stub"]}
            if with_extras else None
        )
        deps = build_default_deps(
            enabled_roles=(
                "planner", "researcher", "tool_executor", "synthesizer",
            ),
            extra_models_by_role=extras,
        )
        return build_council_graph(deps, checkpointer=InMemorySaver())

    def test_tool_executor_has_conditional_fanback_branch(self):
        """The graph builder's branch registry must contain
        ``_fanout_after_tool_executor`` under the ``tool_executor``
        node. Asserts the unconditional edge was replaced."""
        graph = self._build()
        self.assertIn("tool_executor", graph.builder.branches)
        branches = graph.builder.branches["tool_executor"]
        self.assertIn("_fanout_after_tool_executor", branches)

    def test_researcher_branch_still_present(self):
        """The pre-existing ``_route_after_researcher`` conditional
        edge must remain — only the post-tool_executor edge was
        upgraded."""
        graph = self._build()
        self.assertIn("researcher", graph.builder.branches)
        branches = graph.builder.branches["researcher"]
        self.assertIn("_route_after_researcher", branches)


# ====================================================================== #
# 4. Fanback closure behavior — the central #103 routing logic
# ====================================================================== #


@unittest.skipUnless(
    HAS_LANGGRAPH, "langgraph not installed in this env",
)
class TestFanbackAfterToolExecutor(unittest.TestCase):
    """The new ``_fanout_after_tool_executor`` closure: the
    single-researcher path returns the string ``"researcher"``
    (preserving the M6 unconditional-edge semantics); the multi-
    researcher path emits one Send per distinct
    ``parent_lane_idx``.
    """

    def _get_fanback(self, *, with_extras: bool = True):
        """Return the raw fanback function (not the Runnable
        wrapper) so we can drive it with a synthetic state."""
        from tests._parity_helpers import build_default_deps
        from consultants.engine.graph import build_council_graph
        from langgraph.checkpoint.memory import InMemorySaver

        extras = (
            {"researcher": ["extra-1:stub"]}
            if with_extras else None
        )
        deps = build_default_deps(
            enabled_roles=(
                "planner", "researcher", "tool_executor", "synthesizer",
            ),
            extra_models_by_role=extras,
        )
        graph = build_council_graph(deps, checkpointer=InMemorySaver())
        return (
            graph.builder.branches["tool_executor"]
            ["_fanout_after_tool_executor"].path.func
        )

    def test_single_researcher_path_returns_researcher_string(self):
        """All ToolResults carry ``parent_lane_idx=None`` (single-
        researcher non-fanout base-tier path). The fanback must
        return the literal string ``"researcher"`` — identical to
        the old unconditional edge."""
        fanback = self._get_fanback()
        state = {
            "tool_results": [
                ToolResult(intent="a", parent_round=1,
                           parent_lane_idx=None),
                ToolResult(intent="b", parent_round=1,
                           parent_lane_idx=None),
            ],
            "research_rounds_used": 0,
            "plan_items": ["item1"],
        }
        out = fanback(state)
        self.assertEqual(out, "researcher")

    def test_xtier_multi_lane_emits_one_send_per_distinct_parent(self):
        """ToolResults from two researcher lanes
        (parent_lane_idx=0 and =1) — the fanback emits exactly
        two Sends, each routing back to ``researcher`` with the
        correct lane_idx + model_override."""
        fanback = self._get_fanback()
        state = {
            "tool_results": [
                ToolResult(intent="a", parent_round=1, parent_lane_idx=0),
                ToolResult(intent="b", parent_round=1, parent_lane_idx=0),
                ToolResult(intent="c", parent_round=1, parent_lane_idx=1),
            ],
            "research_rounds_used": 0,
            "plan_items": ["item-a", "item-b"],
            "plan": "p",
            "question": "q",
        }
        out = fanback(state)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 2)
        # Sorted by global_idx so the post-mortem is deterministic.
        lane_idxs = [s.arg["lane_idx"] for s in out]
        self.assertEqual(lane_idxs, [0, 1])
        # Both Sends route to "researcher".
        for s in out:
            self.assertEqual(s.node, "researcher")
        # model_override mapping: with [primary, extra] and
        # n_models=2, lane 0 → primary, lane 1 → extra-1.
        self.assertEqual(out[0].arg["model_override"], "stub-model:test")
        self.assertEqual(out[1].arg["model_override"], "extra-1:stub")

    def test_xtier_sends_carry_research_rounds_used_through(self):
        """Each Send slice carries ``research_rounds_used`` from
        state so the per-lane researcher's REPORT-mode call to
        ``tool_results_for_round(state, this_round, ...)`` looks
        up the right round."""
        fanback = self._get_fanback()
        state = {
            "tool_results": [
                ToolResult(intent="a", parent_round=1, parent_lane_idx=0),
            ],
            "research_rounds_used": 0,
            "plan_items": ["x"],
            "plan": "p",
            "question": "q",
        }
        out = fanback(state)
        self.assertIsInstance(out, list)
        for s in out:
            # Carried through, not bumped.
            self.assertEqual(s.arg["research_rounds_used"], 0)
            # Empty deltas so additive reducers don't double-count.
            self.assertEqual(s.arg["turns"], [])
            self.assertEqual(s.arg["total_prompt_tokens"], 0)
            self.assertEqual(s.arg["total_completion_tokens"], 0)

    def test_no_results_at_current_round_returns_researcher(self):
        """A pathological state with no current-round results
        (e.g. malformed checkpoint) degrades to the single-
        researcher path — the unconditional-edge equivalent —
        rather than crashing."""
        fanback = self._get_fanback()
        state = {
            "tool_results": [
                # All in a past round.
                ToolResult(intent="old", parent_round=5,
                           parent_lane_idx=2),
            ],
            "research_rounds_used": 0,  # current_round = 1
            "plan_items": ["x"],
        }
        out = fanback(state)
        self.assertEqual(out, "researcher")

    def test_parent_lane_idx_outside_partition_is_skipped(self):
        """Defensive: a ``parent_lane_idx`` that doesn't map to
        any current partition slot (e.g. plan_items shrank
        mid-cycle) is skipped rather than crashing."""
        fanback = self._get_fanback()
        state = {
            "tool_results": [
                # parent_lane_idx=99 — far outside any reasonable
                # partition for plan_items=["x"] × 2 models.
                ToolResult(intent="a", parent_round=1,
                           parent_lane_idx=99),
            ],
            "research_rounds_used": 0,
            "plan_items": ["x"],  # 1 lane × 2 models = 2 slots
            "plan": "p",
            "question": "q",
        }
        out = fanback(state)
        # All distinct lanes were unreachable, degrade gracefully.
        self.assertEqual(out, "researcher")


# ====================================================================== #
# 5. Router decision rule — _route_after_researcher
# ====================================================================== #


@unittest.skipUnless(
    HAS_LANGGRAPH, "langgraph not installed in this env",
)
class TestRouteAfterResearcher(unittest.TestCase):
    """The post-researcher router emits one Send per unconsumed
    current-round ``ToolPlanItem`` (filtered by the full
    ``(parent_round, lane_idx, parent_lane_idx)`` identity tuple),
    or falls through when no items remain.
    """

    def _get_router(self):
        from tests._parity_helpers import build_default_deps
        from consultants.engine.graph import build_council_graph
        from langgraph.checkpoint.memory import InMemorySaver

        deps = build_default_deps(
            enabled_roles=(
                "planner", "researcher", "tool_executor", "synthesizer",
            ),
            extra_models_by_role={"researcher": ["extra-1:stub"]},
        )
        graph = build_council_graph(deps, checkpointer=InMemorySaver())
        return (
            graph.builder.branches["researcher"]
            ["_route_after_researcher"].path.func
        )

    def test_unconsumed_items_dispatch_one_send_per_item(self):
        """Three ToolPlanItems at the current round, none in
        tool_results — the router emits three Sends, each
        carrying ``parent_lane_idx`` from the originating item."""
        router = self._get_router()
        items = [
            ToolPlanItem(intent="a", lane_idx=0, parent_lane_idx=5),
            ToolPlanItem(intent="b", lane_idx=1, parent_lane_idx=5),
            ToolPlanItem(intent="c", lane_idx=2, parent_lane_idx=5),
        ]
        state = {
            "tool_plan": items,
            "tool_results": [],
            "research_rounds_used": 0,
            "question": "q", "cwd": "/tmp",
            "effort": "xmedium", "models": {}, "topology": "council",
        }
        out = router(state)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 3)
        for s in out:
            self.assertEqual(s.node, "tool_executor")
            self.assertEqual(s.arg["parent_lane_idx"], 5)
        # Each item's lane_idx + parent_lane_idx round-trips into
        # the Send slice so tool_executor_node can stamp them on
        # the resulting ToolResult.
        self.assertEqual(
            [s.arg["lane_idx"] for s in out], [0, 1, 2],
        )

    def test_completed_items_skipped(self):
        """If a ToolResult already exists for an item's
        ``(parent_round, lane_idx, parent_lane_idx)``, the router
        skips that item — handles partial-failure re-entry."""
        router = self._get_router()
        items = [
            ToolPlanItem(intent="a", lane_idx=0, parent_lane_idx=5),
            ToolPlanItem(intent="b", lane_idx=1, parent_lane_idx=5),
        ]
        state = {
            "tool_plan": items,
            "tool_results": [
                # "a" already completed.
                ToolResult(intent="a", parent_round=1,
                           lane_idx=0, parent_lane_idx=5),
            ],
            "research_rounds_used": 0,
            "question": "q", "cwd": "/tmp",
            "effort": "xmedium", "models": {}, "topology": "council",
        }
        out = router(state)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 1)
        # Only the second item dispatched.
        self.assertEqual(out[0].arg["lane_idx"], 1)

    def test_x_tier_sibling_lanes_dont_mask_each_other(self):
        """Critical x-tier invariant: lane 5's completed item
        doesn't mask lane 7's pending item even when they share
        the same plan-item lane_idx."""
        router = self._get_router()
        items = [
            # Both items have lane_idx=0 (each is the first item
            # in their own researcher's plan), but different
            # parent_lane_idx values.
            ToolPlanItem(intent="L5-a", lane_idx=0, parent_lane_idx=5),
            ToolPlanItem(intent="L7-a", lane_idx=0, parent_lane_idx=7),
        ]
        state = {
            "tool_plan": items,
            "tool_results": [
                # Lane 5's first item already done. Lane 7's
                # first item must NOT be masked by this.
                ToolResult(intent="L5-a", parent_round=1,
                           lane_idx=0, parent_lane_idx=5),
            ],
            "research_rounds_used": 0,
            "question": "q", "cwd": "/tmp",
            "effort": "xmedium", "models": {}, "topology": "council",
        }
        out = router(state)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 1)
        # Lane 7's item still dispatches even though both items
        # share lane_idx=0 — the parent_lane_idx in the identity
        # tuple keeps them distinct.
        self.assertEqual(out[0].arg["parent_lane_idx"], 7)
        self.assertEqual(out[0].arg["tool_plan_item"].intent, "L7-a")

    def test_no_items_falls_through(self):
        """Empty tool_plan → router falls through to the next
        natural role (REPORT-mode completion, empty-plan
        fallback, M6 error path — all converge here)."""
        router = self._get_router()
        state = {
            "tool_plan": [],
            "tool_results": [],
            "research_rounds_used": 0,
            "question": "q", "cwd": "/tmp",
            "effort": "xmedium", "models": {}, "topology": "council",
        }
        out = router(state)
        # The fallthrough returns the natural downstream string.
        # Concrete value depends on enabled topology; the contract
        # is "return a string, not a Send list."
        self.assertIsInstance(out, str)


# ====================================================================== #
# 6. M11c-3 dropped state field — awaiting_tool_results is gone
# ====================================================================== #


class TestAwaitingToolResultsDropped(unittest.TestCase):
    """The scalar ``awaiting_tool_results`` flag was removed from
    :class:`CouncilStateV2` by #103. The router now derives the
    dispatch decision from ``tool_plan`` vs ``tool_results``.
    """

    def test_field_not_in_state_annotations(self):
        from consultants.engine.state_v2 import CouncilStateV2
        self.assertNotIn(
            "awaiting_tool_results",
            CouncilStateV2.__annotations__,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
