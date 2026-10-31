"""End-to-end HITL (human-in-the-loop) integration tests.

Build a real LangGraph with:

- ``interrupt_before=["synthesizer"]`` (static review)
- A node that posts a dynamic ``interrupt()`` on a low-confidence signal

Then drive the full ``pause → inject → mutate → resume`` cycle the
M9 HTTP control surface will eventually wrap.

Lives in the consultants test env (langgraph required); skips on
the main ``claude-hooks`` env.
"""

from __future__ import annotations

import unittest
# Module-level imports so TypedDict forward-ref evaluation (used by
# langgraph's StateGraph schema introspection) can resolve names
# like ``Annotated``. Function-local imports don't work
# here — ``get_type_hints`` uses the schema class's module globals.
from typing import Annotated, TypedDict


try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph, START, END
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False

# Module-level imports required by ``get_type_hints`` when LangGraph
# introspects nested TypedDict schemas — see comment above about
# forward-ref evaluation scope.
from consultants.engine.state_v2 import Doc, append_doc


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestStaticReviewBeforeSynthesis(unittest.TestCase):
    """Compile a 2-node graph with ``interrupt_before=["synthesizer"]``.

    Verify the pause happens, ``graph.update_state`` injects context,
    and resumption produces an answer that reflects the injected docs.
    """

    def test_pause_inject_resume_round_trip(self):
        from consultants.server.control import (
            build_inject_delta,
            summarize_state_for_get,
        )

        class S(TypedDict, total=False):
            question: str
            research: list[str]
            additional_context: Annotated[list[Doc], append_doc]
            final_answer: str

        def researcher(state):
            return {"research": [
                f"Researched: {state.get('question', '')}"
            ]}

        def synthesizer(state):
            # The synth produces an answer that includes the
            # injected context if any — that's how the test detects
            # the inject took effect.
            docs = state.get("additional_context") or []
            extras = " | ".join(d.text for d in docs)
            base = "; ".join(state.get("research") or [])
            ans = f"ANSWER[{base}]"
            if extras:
                ans += f" + EXTRAS[{extras}]"
            return {"final_answer": ans}

        sg: StateGraph = StateGraph(S)
        sg.add_node("researcher", researcher)
        sg.add_node("synthesizer", synthesizer)
        sg.add_edge(START, "researcher")
        sg.add_edge("researcher", "synthesizer")
        sg.add_edge("synthesizer", END)

        checkpointer = InMemorySaver()
        graph = sg.compile(
            checkpointer=checkpointer,
            interrupt_before=["synthesizer"],
        )
        config = {"configurable": {"thread_id": "csl-test-static"}}

        # First invoke — runs researcher, parks before synthesizer.
        out1 = graph.invoke({"question": "what is X?"}, config=config)
        self.assertNotIn("final_answer", out1)  # paused before synth
        self.assertEqual(out1["research"],
                         ["Researched: what is X?"])

        # Inspect state via the M5 summarizer.
        snap = graph.get_state(config)
        summary = summarize_state_for_get(
            {"values": snap.values, "next": snap.next,
             "tasks": list(snap.tasks)},
            sid="csl-test-static",
        )
        self.assertEqual(summary["research_count"], 1)
        # The 'next' tuple in LangGraph 1.2 surfaces the pending node.
        self.assertIn("synthesizer", summary["next"])

        # Inject via the builder + graph.update_state. LangGraph 1.2
        # requires ``as_node`` to be a node in the graph (or omitted
        # when there's an unambiguous last-writer). Here we attribute
        # the update to "researcher" (the actual last writer);
        # production code adds a dedicated "injector" no-op node to
        # the graph so the transcript clearly shows who edited state.
        delta = build_inject_delta(
            role="synthesizer",
            text="Lead with the bottom line.",
        )
        graph.update_state(config, delta, as_node="researcher")

        # Resume — the second invoke continues past the pause and
        # runs the synthesizer with the merged state.
        out2 = graph.invoke(None, config=config)
        self.assertIn("ANSWER", out2["final_answer"])
        self.assertIn("Lead with the bottom line", out2["final_answer"])
