"""End-to-end M6b: real LangGraph integration of the tool_executor
role.

Builds a minimal council where:

- The planner emits a 1-item plan (forces the non-fanout path).
- The researcher operates in PLAN mode on first entry, emits a
  ``tool_plan`` JSON block of 2 items.
- The graph's M6 conditional edge fans out 2 ``Send`` lanes to
  the tool_executor node.
- Each lane consumes a ``ToolPlanItem`` and returns a
  ``ToolResult`` via the additive reducer.
- An unconditional edge brings us back to the researcher, which
  enters REPORT mode and weaves the tool_results into its
  research output.
- Critic + synthesizer run as usual.

All chat calls are stubbed via fake ChatClients keyed on the role,
so this exercises the topology + state-channel reducers without
needing a live Ollama upstream.

Lives in the consultants test env (langgraph required); skips
on the main ``claude-hooks`` env.
"""

from __future__ import annotations

import json
import unittest


try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph, START, END
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestToolExecutorE2E(unittest.TestCase):

    def test_plan_mode_emits_fanout_then_report_writes_research(self):
        from consultants.engine.graph import (
            GraphDeps, build_council_graph,
        )

        # Per-role chat-client replies. The planner returns a 1-item
        # plan (single-researcher path). The researcher returns a
        # JSON tool_plan on first entry (PLAN mode), then a final
        # research-report blob on second entry (REPORT mode).
        researcher_calls: list[dict] = []

        class _PlannerStub:
            def chat(self, payload, *, think=True):
                return _resp("1. investigate auth flow")

        class _ResearcherStub:
            def chat(self, payload, *, think=True):
                researcher_calls.append(payload)
                if len(researcher_calls) == 1:
                    # PLAN mode response — fenced JSON.
                    return _resp(
                        "Here's the plan:\n```json\n"
                        + json.dumps({
                            "tool_plan": [
                                {"intent": "find login() impl",
                                 "why": "audit"},
                                {"intent": "list session middleware",
                                 "why": "audit"},
                            ],
                        })
                        + "\n```",
                    )
                # REPORT mode response.
                return _resp(
                    "RESEARCH REPORT: login() at auth.py:42; "
                    "session middleware: SessionMiddleware at "
                    "middleware.py:17.",
                )

        class _CriticStub:
            def chat(self, payload, *, think=True):
                return _resp("READY: research is sufficient.")

        class _SynthesizerStub:
            def chat(self, payload, *, think=True):
                return _resp("FINAL ANSWER: auth flow audited.")

        # tool_executor's ChatClient is unused — the stub
        # loop_runner doesn't call it.
        class _ToolExecChatStub:
            def chat(self, payload, *, think=False):  # pragma: no cover
                raise AssertionError("should not be called when "
                                      "loop_runner stub is used")

        chat_clients = {
            "planner": _PlannerStub(),
            "researcher": _ResearcherStub(),
            "critic": _CriticStub(),
            "synthesizer": _SynthesizerStub(),
            "tool_executor": _ToolExecChatStub(),
        }

        # Stub the loop_runner inside tool_executor_node by monkey-
        # patching the module so each lane returns a deterministic
        # ToolResult. We do this by wrapping the actual node.
        executed_intents: list[str] = []

        def _fake_loop_runner(payload, cwd, *, config, tool_specs,
                               chat_fn, tool_executor,
                               on_iter=None, on_tool=None,
                               preseed_builder=None):
            # Extract the intent from the payload's user message.
            user_msg = payload["messages"][-1]["content"]
            intent = ""
            for line in user_msg.split("\n"):
                if line.startswith("find ") or "login" in line \
                        or "session" in line:
                    intent = line
                    break
            executed_intents.append(intent)
            if on_tool is not None:
                on_tool("read_file", '{"path":"x"}', "content", 5, None)
            return {"final": {"choices": [
                {"message": {"content":
                              f"EVIDENCE for: {intent}"}},
            ]}}

        # Patch tool_executor_node to use the fake runner by name.
        import consultants.engine.tool_executor as te_mod
        original = te_mod.tool_executor_node

        def _patched_node(state, *, chat_client, tool_executor,
                          tool_specs, grounding_msgs, model, cwd,
                          think=False, loop_runner=None,
                          recorder=None):
            return original(
                state,
                chat_client=chat_client,
                tool_executor=tool_executor,
                tool_specs=tool_specs,
                grounding_msgs=grounding_msgs,
                model=model, cwd=cwd, think=think,
                loop_runner=_fake_loop_runner,
                recorder=recorder,
            )
        te_mod.tool_executor_node = _patched_node
        self.addCleanup(setattr, te_mod,
                         "tool_executor_node", original)

        # Build GraphDeps with tool_executor enabled.
        deps = GraphDeps(
            chat_clients=chat_clients,
            models={
                "planner": "kimi:cloud",
                "researcher": "kimi:cloud",
                "tool_executor": "gemma4:31b-cloud",
                "critic": "kimi:cloud",
                "synthesizer": "kimi:cloud",
            },
            enabled_roles=(
                "planner", "researcher", "tool_executor",
                "critic", "synthesizer",
            ),
            cwd="/tmp",
            tool_executor=lambda *a, **kw: "",
            tool_specs=[],
            grounding_msgs=[],
            disable_cache=True,
        )

        graph = build_council_graph(
            deps, checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "csl-m6b-e2e"}}

        initial = {
            "question": "audit the auth flow",
            "cwd": "/tmp",
            "models": deps.models,
            "topology": "council",
            "effort": "medium",
        }
        out = graph.invoke(initial, config=config, debug=False)

        # 1. The researcher chat was called twice — PLAN then REPORT.
        self.assertEqual(len(researcher_calls), 2)
        # First call's user message contains the PLAN_MODE marker.
        self.assertIn(
            "TOOL-EXECUTOR MODE", researcher_calls[0]["messages"][-1]["content"],
        )
        # Second call's user message contains the PRIOR TOOL
        # RESULTS block.
        self.assertIn(
            "PRIOR TOOL RESULTS",
            researcher_calls[1]["messages"][-1]["content"],
        )

        # 2. Two tool_executor lanes ran (one per plan item).
        self.assertEqual(len(executed_intents), 2)

        # 3. The final answer reflects the REPORT-mode research.
        self.assertIn("FINAL ANSWER", out["final_answer"])

        # 4. tool_results channel has both lane outputs merged.
        results = out.get("tool_results") or []
        self.assertEqual(len(results), 2)
        # Both lanes share parent_round=1 (the PLAN-mode researcher's
        # first round).
        self.assertTrue(all(r.parent_round == 1 for r in results))

    def test_tool_executor_skipped_when_role_disabled(self):
        """Regression guard: without tool_executor in enabled_roles,
        researcher uses the classic inline tool subloop and no
        PLAN/REPORT alternation happens."""
        from consultants.engine.graph import (
            GraphDeps, build_council_graph,
        )

        researcher_calls: list[dict] = []

        class _ResearcherStub:
            def chat(self, payload, *, think=True):
                researcher_calls.append(payload)
                return _resp("inline research report")

        class _Stub:
            def chat(self, payload, *, think=True):
                return _resp("ok")

        deps = GraphDeps(
            chat_clients={
                "planner": _Stub(),
                "researcher": _ResearcherStub(),
                "synthesizer": _Stub(),
            },
            models={
                "planner": "m", "researcher": "m", "synthesizer": "m",
            },
            enabled_roles=("planner", "researcher", "synthesizer"),
            cwd="/tmp",
            tool_executor=lambda *a, **kw: "",
            tool_specs=[],
            grounding_msgs=[],
            disable_cache=True,
        )
        graph = build_council_graph(
            deps, checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "csl-no-te"}}
        out = graph.invoke(
            {"question": "Q", "cwd": "/tmp",
             "models": deps.models, "topology": "council",
             "effort": "medium"},
            config=config,
        )
        # No PLAN/REPORT — researcher fired once, no plan-mode
        # marker in its user message.
        self.assertEqual(len(researcher_calls), 1)
        self.assertNotIn(
            "TOOL-EXECUTOR MODE",
            researcher_calls[0]["messages"][-1]["content"],
        )
        # No tool_plan / tool_results channels populated.
        self.assertEqual(out.get("tool_plan") or [], [])
        self.assertEqual(out.get("tool_results") or [], [])


def _resp(content: str) -> dict:
    """Stub ChatClient response shape."""
    return {
        "choices": [{
            "role": "assistant",
            "message": {"role": "assistant", "content": content},
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


if __name__ == "__main__":
    unittest.main()
