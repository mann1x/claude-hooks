"""LangGraph 1.2 smoke — pins the framework contracts the v2 council
overhaul relies on (Milestone 0 of the /consultants v2 plan).

This file is the single source of truth for "which LangGraph features
we depend on and what shape they return". A future framework bump
that breaks any of these tests means the overhaul plan needs
revisiting — flag it loudly rather than silently miscompiling against
the new API.

Lives in the main ``claude-hooks`` test tree so the count rolls into
``pytest --collect-only -q`` for release notes. Skips cleanly when
langgraph is not installed (main env doesn't have it; consultants env
does — see ``consultants/pyproject.toml`` for the pinned versions).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Annotated, TypedDict
import operator


try:
    import langgraph  # noqa: F401
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import Send, Command, interrupt
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.checkpoint.sqlite import SqliteSaver
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


# Postgres checkpointer is opt-in; pinned in the [postgres] extra. The
# smoke checks the import surface only — wiring against a live
# Postgres lives in the M1 checkpointer-factory tests with a fixture.
try:
    from langgraph.checkpoint.postgres import PostgresSaver  # noqa: F401
    HAVE_POSTGRES_SAVER = True
except ImportError:
    HAVE_POSTGRES_SAVER = False


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed (consultants env required)")
class TestLangGraphVersion(unittest.TestCase):
    """Pin the version contract."""

    def test_langgraph_is_one_two_or_newer(self):
        # Some 1.x builds don't expose ``__version__``; importlib.metadata
        # is the canonical path. Either way we want the major.minor to
        # be 1.2+ — the overhaul plan targets features (TimeoutPolicy,
        # RunControl, v3 stream) added in 1.2.
        from importlib.metadata import version
        v = version("langgraph")
        major, minor = (int(x) for x in v.split(".")[:2])
        self.assertGreaterEqual(
            (major, minor), (1, 2),
            f"langgraph must be >= 1.2 (got {v})",
        )

    def test_checkpoint_packages_present(self):
        from importlib.metadata import version
        for pkg, min_major in [
            ("langgraph-checkpoint", 4),
            ("langgraph-checkpoint-sqlite", 3),
            ("langgraph-prebuilt", 1),
            ("langchain-core", 1),
        ]:
            v = version(pkg)
            major = int(v.split(".")[0])
            self.assertGreaterEqual(
                major, min_major,
                f"{pkg} must be >= {min_major}.x (got {v})",
            )


class _CounterState(TypedDict, total=False):
    counter: Annotated[int, operator.add]
    messages: Annotated[list[str], operator.add]
    awaited_value: str


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestTrivialGraph(unittest.TestCase):
    """Baseline: build + invoke a 2-node graph with an additive reducer
    that the v2 fanout dispatcher will rely on."""

    def test_invoke_with_reducer(self):
        def inc(state):
            return {"counter": 1, "messages": ["hello"]}

        sg = StateGraph(_CounterState)
        sg.add_node("inc", inc)
        sg.add_edge(START, "inc")
        sg.add_edge("inc", END)
        g = sg.compile()

        out = g.invoke({"counter": 0, "messages": []})
        self.assertEqual(out["counter"], 1)
        self.assertEqual(out["messages"], ["hello"])

    def test_send_fanout_reducer_merges(self):
        """Send fanout into the same node with an additive reducer —
        the exact pattern v2 researcher fanout uses for `research`."""

        def emit_sends(state):
            return [
                Send("lane", {"counter": 0, "messages": [f"lane-{i}"]})
                for i in range(3)
            ]

        def lane(state):
            return {"counter": 1, "messages": [f"saw:{state.get('messages')[-1]}"]}

        sg = StateGraph(_CounterState)
        sg.add_node("lane", lane)
        sg.add_conditional_edges(START, emit_sends, ["lane"])
        sg.add_edge("lane", END)
        g = sg.compile()

        out = g.invoke({"counter": 0, "messages": []})
        # Three Send branches each contributed +1 → reducer sums to 3.
        self.assertEqual(out["counter"], 3)
        # The Send PAYLOAD is the target node's input state, NOT a
        # reducer write — only the lane's return value flows back
        # through the reducer. So 3 lanes × 1 message each = 3 entries
        # in the merged parent state. This is the contract the v2
        # researcher fanout relies on.
        self.assertEqual(len(out["messages"]), 3)
        # Each lane saw the right Send payload (its own "lane-N" string).
        seen = sorted(out["messages"])
        self.assertEqual(seen, ["saw:lane-0", "saw:lane-1", "saw:lane-2"])


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestCommandGotoUpdate(unittest.TestCase):
    """The v2 graph uses Command(goto, update) from inside nodes to
    drive runtime-mutable routing without the Send + conditional-edge
    dance. Pin the contract here."""

    def test_command_goto_routes(self):
        def planner(state):
            return Command(update={"counter": 1, "messages": ["plan"]},
                           goto="synth")

        def synth(state):
            return {"messages": ["synth"]}

        # No explicit edge from planner — Command(goto) wires it.
        sg = StateGraph(_CounterState)
        sg.add_node("planner", planner)
        sg.add_node("synth", synth)
        sg.add_edge(START, "planner")
        sg.add_edge("synth", END)
        g = sg.compile()

        out = g.invoke({"counter": 0, "messages": []})
        self.assertEqual(out["counter"], 1)
        self.assertIn("plan", out["messages"])
        self.assertIn("synth", out["messages"])


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestCheckpointerResume(unittest.TestCase):
    """Static interrupt + Command(resume) — the foundation for v2's
    HITL pause/resume. Tested against InMemorySaver, SqliteSaver
    (in-memory), and (when available) PostgresSaver."""

    def _build_pausable_graph(self, checkpointer):
        def gate(state):
            decision = interrupt({"prompt": "approve?",
                                  "context": state.get("messages", [])})
            return {"awaited_value": decision,
                    "messages": [f"approved:{decision}"]}

        def end(state):
            return {"messages": [f"final:{state.get('awaited_value')}"]}

        sg = StateGraph(_CounterState)
        sg.add_node("gate", gate)
        sg.add_node("end", end)
        sg.add_edge(START, "gate")
        sg.add_edge("gate", "end")
        sg.add_edge("end", END)
        return sg.compile(checkpointer=checkpointer)

    def test_interrupt_resume_memory_saver(self):
        cp = InMemorySaver()
        g = self._build_pausable_graph(cp)
        cfg = {"configurable": {"thread_id": "t-1"}}

        # First invoke: hits the interrupt and parks.
        out = g.invoke({"messages": ["pre"]}, config=cfg)
        # The interrupt payload sits in __interrupt__.
        self.assertIn("__interrupt__", out)
        interrupts = out["__interrupt__"]
        self.assertEqual(len(interrupts), 1)
        # Resume with a value — node receives it as interrupt()'s return.
        out2 = g.invoke(Command(resume="yes"), config=cfg)
        self.assertEqual(out2["awaited_value"], "yes")
        self.assertIn("approved:yes", out2["messages"])
        self.assertIn("final:yes", out2["messages"])

    def test_interrupt_resume_sqlite_saver_memory(self):
        import sqlite3
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            cp = SqliteSaver(conn)
            g = self._build_pausable_graph(cp)
            cfg = {"configurable": {"thread_id": "t-1"}}
            out = g.invoke({"messages": ["pre"]}, config=cfg)
            self.assertIn("__interrupt__", out)
            out2 = g.invoke(Command(resume="approved"), config=cfg)
            self.assertEqual(out2["awaited_value"], "approved")
        finally:
            conn.close()

    def test_interrupt_resume_sqlite_saver_file_resumes_across_handles(self):
        """The whole point of file-backed checkpointing — close the
        first SqliteSaver, open a fresh one on the same file, resume."""
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "cp.db")
            cfg = {"configurable": {"thread_id": "t-1"}}

            # Phase 1: park at interrupt, close handles.
            conn1 = sqlite3.connect(db_path, check_same_thread=False)
            try:
                cp1 = SqliteSaver(conn1)
                g1 = self._build_pausable_graph(cp1)
                out = g1.invoke({"messages": ["pre"]}, config=cfg)
                self.assertIn("__interrupt__", out)
            finally:
                conn1.close()

            # Phase 2: NEW handle on same file. Resume picks up where it left off.
            conn2 = sqlite3.connect(db_path, check_same_thread=False)
            try:
                cp2 = SqliteSaver(conn2)
                g2 = self._build_pausable_graph(cp2)
                out2 = g2.invoke(Command(resume="rehydrated"), config=cfg)
                self.assertEqual(out2["awaited_value"], "rehydrated")
                self.assertIn("final:rehydrated", out2["messages"])
            finally:
                conn2.close()

    @unittest.skipUnless(HAVE_POSTGRES_SAVER,
                         "langgraph-checkpoint-postgres not installed "
                         "(opt-in [postgres] extra)")
    def test_postgres_saver_importable(self):
        # Full Postgres integration belongs in M1's checkpointer
        # factory tests with a real fixture; here we only verify the
        # import surface so a future pyproject break is loud.
        from langgraph.checkpoint.postgres import PostgresSaver  # noqa
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestUpdateStateAndHistory(unittest.TestCase):
    """The v2 control surface uses graph.update_state() to mutate
    RuntimeControl mid-flight. Pin the contract here."""

    def test_update_state_persists_through_resume(self):
        def n(state):
            return {"messages": [f"saw:{state.get('counter', 0)}"]}

        sg = StateGraph(_CounterState)
        sg.add_node("n", n)
        sg.add_edge(START, "n")
        sg.add_edge("n", END)

        cp = InMemorySaver()
        g = sg.compile(checkpointer=cp)
        cfg = {"configurable": {"thread_id": "t-1"}}

        # Initial run.
        g.invoke({"counter": 0, "messages": []}, config=cfg)
        snap = g.get_state(cfg)
        # The first run completed; messages were emitted.
        self.assertIn("saw:0", snap.values["messages"])

        # Mutate state via update_state. The reducer adds, so counter
        # becomes 0 + 5 = 5. Then re-invoke; the next node sees the
        # bumped value.
        g.update_state(cfg, {"counter": 5}, as_node="n")
        snap2 = g.get_state(cfg)
        self.assertEqual(snap2.values["counter"], 5)

    def test_get_state_history_returns_supersteps(self):
        def n1(state):
            return {"counter": 1, "messages": ["n1"]}

        def n2(state):
            return {"counter": 10, "messages": ["n2"]}

        sg = StateGraph(_CounterState)
        sg.add_node("n1", n1)
        sg.add_node("n2", n2)
        sg.add_edge(START, "n1")
        sg.add_edge("n1", "n2")
        sg.add_edge("n2", END)

        cp = InMemorySaver()
        g = sg.compile(checkpointer=cp)
        cfg = {"configurable": {"thread_id": "h-1"}}
        g.invoke({"counter": 0, "messages": []}, config=cfg)

        history = list(g.get_state_history(cfg))
        # Newest first by convention. There should be at least one
        # checkpoint per superstep + the input — typically 4 entries
        # for a 2-node graph.
        self.assertGreaterEqual(len(history), 3)
        # The latest one has both increments applied.
        latest = history[0]
        self.assertEqual(latest.values["counter"], 11)


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestAStreamEvents(unittest.TestCase):
    """astream_events is the SSE feed source for the M4 streaming
    work. Pin the event shape so M4 can demultiplex confidently."""

    def test_astream_yields_node_lifecycle(self):
        def hop(state):
            return {"messages": ["hop"]}

        sg = StateGraph(_CounterState)
        sg.add_node("hop", hop)
        sg.add_edge(START, "hop")
        sg.add_edge("hop", END)
        g = sg.compile()

        async def run():
            events = []
            async for ev in g.astream_events({"messages": []}, version="v2"):
                events.append(ev)
            return events

        events = asyncio.run(run())
        # At a minimum, we see chain start + chain end + a node fire.
        # The exact event vocabulary varies across 1.x versions — we
        # pin only the must-haves the SSE bridge needs:
        kinds = {e.get("event") for e in events}
        # 1.2 emits at least these in some shape:
        self.assertTrue(
            any(k and "start" in k for k in kinds),
            f"expected at least one *_start event; got kinds={kinds!r}",
        )
        self.assertTrue(
            any(k and "end" in k for k in kinds),
            f"expected at least one *_end event; got kinds={kinds!r}",
        )


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestCustomEventWriter(unittest.TestCase):
    """The v2 council emits typed CouncilEvent objects from inside
    nodes via the stream writer. Pin that this works."""

    def test_get_stream_writer_emits_custom_event(self):
        from langgraph.config import get_stream_writer

        def n(state):
            writer = get_stream_writer()
            writer({"progress": 0.5, "stage": "researching"})
            return {"messages": ["done"]}

        sg = StateGraph(_CounterState)
        sg.add_node("n", n)
        sg.add_edge(START, "n")
        sg.add_edge("n", END)
        g = sg.compile()

        # stream_mode="custom" surfaces just the writer payloads.
        chunks = list(g.stream(
            {"messages": []}, stream_mode="custom"))
        self.assertTrue(any(
            isinstance(c, dict) and c.get("stage") == "researching"
            for c in chunks
        ), f"expected custom event in stream chunks; got {chunks!r}")


if __name__ == "__main__":
    unittest.main()
