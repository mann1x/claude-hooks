"""End-to-end M8: shared-store recall surfaces in a real LangGraph
researcher prompt, and records the researcher's report back so
subsequent invocations on the same store can see it.

Two distinct scenarios:

1. ``test_researcher_recalls_pre_seeded_finding`` — Pre-seed the
   store with a finding tagged as if from a "lane 0" earlier
   invocation. Run a single-researcher graph (no fanout) on a
   plan-item that matches the seed query. Assert the chat payload
   the researcher sees contains the seeded finding (proves
   ``recall_research`` -> ``format_findings_block`` -> message
   build is end-to-end wired).

2. ``test_researcher_records_finding_to_store`` — Same single-
   researcher graph, no pre-seed. After invoke completes, the
   store contains exactly one entry under ``(sid, "research")``
   with the researcher's report text and the correct lane/plan_item
   metadata (proves ``record_research`` fires on the v1 inline-loop
   success path).

Skips on the main ``claude-hooks`` env (langgraph required).
"""

from __future__ import annotations

import unittest


try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


def _resp(content: str) -> dict:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestStoreE2ERecall(unittest.TestCase):

    def test_researcher_recalls_pre_seeded_finding(self):
        """Pre-seeded peer finding lands in the researcher's prompt."""
        from consultants.engine.graph import (
            GraphDeps, build_council_graph,
        )
        from consultants.engine.store import (
            Namespaces, record_research,
        )

        store = InMemoryStore()
        sid = "csl-store-e2e-recall"

        # Pre-seed: a "peer lane 0" finding tagged for the same
        # plan-item the researcher about to run will work on.
        record_research(
            store, sid,
            lane_idx=0, plan_item="audit auth flow",
            finding="CSRF vector exists on /api/logout — POST accepts "
                    "GET in dev mode (see auth/csrf.py:42).",
        )

        # Capture chat payloads so we can introspect what the
        # researcher actually saw.
        seen: dict[str, list[dict]] = {"researcher": [], "synthesizer": []}

        class _CapturingStub:
            def __init__(self, role, content):
                self.role = role
                self.content = content

            def chat(self, payload, *, think=True):
                seen[self.role].append(payload)
                return _resp(self.content)

        class _PlannerStub:
            def chat(self, payload, *, think=True):
                # 1 plan item -> 1-lane research (no fanout, so
                # state.lane_idx is None -> peer-findings dedup
                # doesn't filter our seed out).
                return _resp("1. audit auth flow")

        class _CriticStub:
            def chat(self, payload, *, think=True):
                return _resp("DECISION: ready")

        deps = GraphDeps(
            chat_clients={
                "planner": _PlannerStub(),
                "researcher": _CapturingStub(
                    "researcher",
                    "Research report: confirmed CSRF vector at "
                    "auth/csrf.py:42.",
                ),
                "critic": _CriticStub(),
                "synthesizer": _CapturingStub(
                    "synthesizer", "FINAL ANSWER: audited.",
                ),
            },
            models={r: "m" for r in (
                "planner", "researcher", "critic", "synthesizer")},
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
            cwd="/tmp",
            tool_executor=lambda *a, **kw: "",
            tool_specs=[], grounding_msgs=[], disable_cache=True,
            store=store, sid=sid,
        )
        graph = build_council_graph(
            deps, checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": sid}}

        initial = {
            "question": "audit auth",
            "cwd": "/tmp",
            "models": deps.models,
            "topology": "council",
            "effort": "medium",
        }
        out = graph.invoke(initial, config=config)

        # Researcher fired exactly once.
        self.assertEqual(len(seen["researcher"]), 1)
        researcher_payload = seen["researcher"][0]
        # Final user message carries the peer-findings block.
        user_msg = researcher_payload["messages"][-1]
        self.assertEqual(user_msg["role"], "user")
        self.assertIn("Peer findings", user_msg["content"])
        self.assertIn("CSRF vector", user_msg["content"])
        self.assertIn("auth/csrf.py:42", user_msg["content"])

        # The synthesizer's answer composed -- proves the graph
        # didn't fault on the recall path.
        self.assertIn("FINAL ANSWER", out["final_answer"])

    def test_researcher_records_finding_to_store(self):
        """Researcher's report lands under (sid, 'research') after invoke."""
        from consultants.engine.graph import (
            GraphDeps, build_council_graph,
        )
        from consultants.engine.store import Namespaces

        store = InMemoryStore()
        sid = "csl-store-e2e-record"

        # No pre-seed: the only entry afterwards must be the
        # researcher's own report.
        before = list(store.search(Namespaces.research(sid)))
        self.assertEqual(before, [])

        class _Stub:
            def __init__(self, content="ok"):
                self.content = content

            def chat(self, payload, *, think=True):
                return _resp(self.content)

        report_text = (
            "Research report: traced auth flow. Two issues — "
            "(1) CSRF on /logout, (2) JWT not rotating on refresh."
        )
        deps = GraphDeps(
            chat_clients={
                "planner": _Stub("1. audit auth"),
                "researcher": _Stub(report_text),
                "critic": _Stub("DECISION: ready"),
                "synthesizer": _Stub("FINAL ANSWER: audited."),
            },
            models={r: "m" for r in (
                "planner", "researcher", "critic", "synthesizer")},
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
            cwd="/tmp",
            tool_executor=lambda *a, **kw: "",
            tool_specs=[], grounding_msgs=[], disable_cache=True,
            store=store, sid=sid,
        )
        graph = build_council_graph(
            deps, checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": sid}}

        graph.invoke({
            "question": "audit auth",
            "cwd": "/tmp",
            "models": deps.models,
            "topology": "council",
            "effort": "medium",
        }, config=config)

        # Exactly one finding recorded under the session namespace.
        after = list(store.search(Namespaces.research(sid)))
        self.assertEqual(len(after), 1)
        rec = after[0]
        self.assertEqual(rec.value["text"], report_text)
        # lane_idx is None for the non-fanout path.
        self.assertIsNone(rec.value["lane_idx"])

    def test_no_store_means_no_findings_block(self):
        """When deps.store is None, the researcher's prompt has no
        peer-findings block — verifies the zero-cost path."""
        from consultants.engine.graph import (
            GraphDeps, build_council_graph,
        )

        seen_msgs: list[dict] = []

        class _Stub:
            def __init__(self, content="ok"):
                self.content = content

            def chat(self, payload, *, think=True):
                seen_msgs.append(payload)
                return _resp(self.content)

        deps = GraphDeps(
            chat_clients={
                "planner": _Stub("1. audit auth"),
                "researcher": _Stub("Research report: …"),
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
            # store=None (default), sid=None (default) -> no recall
        )
        graph = build_council_graph(
            deps, checkpointer=InMemorySaver(),
        )
        graph.invoke({
            "question": "Q",
            "cwd": "/tmp",
            "models": deps.models,
            "topology": "council",
            "effort": "medium",
        }, config={"configurable": {"thread_id": "csl-nostore"}})

        # Find the researcher payload (planner/synthesizer also
        # appear in seen_msgs). The researcher's user message must
        # NOT contain the peer-findings block.
        researcher_user_msgs = [
            p["messages"][-1]["content"]
            for p in seen_msgs
            if "PLAN FROM PLANNER" in p["messages"][-1]["content"]
        ]
        self.assertEqual(len(researcher_user_msgs), 1)
        self.assertNotIn("Peer findings", researcher_user_msgs[0])


if __name__ == "__main__":
    unittest.main()
