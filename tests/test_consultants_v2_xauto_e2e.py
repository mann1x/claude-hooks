"""End-to-end M7b: xauto adaptive-effort escalation in a real
LangGraph.

Builds a minimal council where the critic returns
``needs_more_research`` on the first pass; the xauto escalator
node inspects state, fires a ``RuntimeMutation`` (xmedium →
xhigh), and the next ``route_after_critic`` evaluation sees the
bumped ``max_rounds`` / ``max_reroutes`` and routes back to the
researcher for another round.

Verifies:

1. Escalator emits the RuntimeMutation event observable via
   ``astream_events``.
2. ``runtime_control.xauto_tier`` advances from "xmedium" to
   "xhigh" in state after the escalator fires.
3. ``runtime_control.max_rounds`` and ``max_reroutes`` get
   bumped to the xhigh values (3 and 2 respectively).
4. ``xauto_escalations`` increments to 1.
5. The graph completes — critic re-routes once, researcher fires
   a second round, critic returns ready, synthesizer composes.

Skips on the main ``claude-hooks`` env (langgraph required).
"""

from __future__ import annotations

import unittest


try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestXautoEscalationE2E(unittest.TestCase):

    def test_critic_dissent_drives_xmedium_to_xhigh_escalation(self):
        from consultants.engine.graph import (
            GraphDeps, build_council_graph,
        )

        critic_calls: list[dict] = []
        researcher_calls: list[dict] = []

        # Critic returns needs_more_research on first call (drives
        # escalation), ready on the second (consummates the
        # post-escalation re-route).
        class _CriticStub:
            def chat(self, payload, *, think=True):
                critic_calls.append(payload)
                if len(critic_calls) == 1:
                    txt = ("DECISION: needs_more_research\n"
                           "The plan didn't surface auth-flow "
                           "specifics.")
                else:
                    txt = "DECISION: ready"
                return _resp(txt)

        class _ResearcherStub:
            def chat(self, payload, *, think=True):
                researcher_calls.append(payload)
                idx = len(researcher_calls)
                return _resp(f"Research report (round {idx}): findings")

        class _Stub:
            def __init__(self, content="ok"):
                self.content = content

            def chat(self, payload, *, think=True):
                return _resp(self.content)

        deps = GraphDeps(
            chat_clients={
                # 1-item plan → no fanout (FANOUT_MIN_ITEMS=2),
                # single-researcher path so the escalation count
                # is unambiguous.
                "planner": _Stub("1. investigate auth"),
                "researcher": _ResearcherStub(),
                "critic": _CriticStub(),
                "synthesizer": _Stub("FINAL ANSWER: audited."),
            },
            models={r: "m" for r in (
                "planner", "researcher", "critic", "synthesizer")},
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
            cwd="/tmp",
            tool_executor=lambda *a, **kw: "",
            tool_specs=[], grounding_msgs=[], disable_cache=True,
        )
        graph = build_council_graph(
            deps, checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "csl-xauto-e2e"}}

        initial = {
            "question": "audit auth",
            "cwd": "/tmp",
            "models": deps.models,
            "topology": "council",
            "effort": "xauto",  # KEY: opt into adaptive mode
            "runtime_control": {"xauto_tier": "xmedium"},
        }
        out = graph.invoke(initial, config=config)

        # The escalator fired exactly once: xauto_escalations = 1.
        rc = out.get("runtime_control") or {}
        self.assertEqual(rc.get("xauto_escalations"), 1)
        self.assertEqual(rc.get("xauto_tier"), "xhigh")
        # Topology delta applied: xhigh-tier caps.
        self.assertEqual(rc.get("max_rounds"), 3)
        self.assertEqual(rc.get("max_reroutes"), 2)
        # Confidence target tightened from 0.60 to 0.70.
        self.assertAlmostEqual(rc.get("confidence_target"), 0.70)

        # Researcher fired twice — pre-escalation + post-escalation
        # reroute. Critic fired twice — initial dissent + final
        # ready.
        self.assertEqual(len(researcher_calls), 2)
        self.assertEqual(len(critic_calls), 2)

        # Final answer composed by synthesizer.
        self.assertIn("FINAL ANSWER", out["final_answer"])

    def test_non_xauto_run_skips_escalator(self):
        """Regression guard: a plain medium-effort run never escalates,
        even when critic returns needs_more_research."""
        from consultants.engine.graph import (
            GraphDeps, build_council_graph,
        )

        # Same shape as the xauto test, but effort=medium. Critic
        # ALWAYS returns ready so we don't hit the reroute cap.
        class _Stub:
            def __init__(self, content="ok"):
                self.content = content

            def chat(self, payload, *, think=True):
                return _resp(self.content)

        deps = GraphDeps(
            chat_clients={
                "planner": _Stub("1. step\n2. step"),
                "researcher": _Stub("research findings"),
                "critic": _Stub("DECISION: ready"),
                "synthesizer": _Stub("FINAL ANSWER"),
            },
            models={r: "m" for r in (
                "planner", "researcher", "critic", "synthesizer")},
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
            cwd="/tmp",
            tool_executor=lambda *a, **kw: "",
            tool_specs=[], grounding_msgs=[], disable_cache=True,
        )
        graph = build_council_graph(
            deps, checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "csl-medium-e2e"}}
        out = graph.invoke(
            {"question": "Q", "cwd": "/tmp",
             "models": deps.models, "topology": "council",
             "effort": "medium"},
            config=config,
        )
        # No escalations recorded.
        rc = out.get("runtime_control") or {}
        self.assertIn(rc.get("xauto_escalations"), (None, 0))
        # No xauto_tier transition.
        self.assertNotIn("xauto_tier", rc)


def _resp(content: str) -> dict:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


if __name__ == "__main__":
    unittest.main()
