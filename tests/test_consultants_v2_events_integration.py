"""End-to-end integration: ``emit()`` inside a real LangGraph node
surfaces on ``astream_events(version="v2")`` and round-trips through
the SSE bridge.

Lives in the consultants test env (langgraph required); skips on
the main ``claude-hooks`` env.
"""

from __future__ import annotations

import asyncio
import json
import unittest


try:
    from langgraph.graph import StateGraph, START, END
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestEmitToAstreamEventsRoundTrip(unittest.TestCase):
    """A node calls ``emit(NodeStarted(...))``; the compiled graph's
    ``astream_events`` stream surfaces it as ``on_custom_event``;
    the SSE bridge demuxes it into ``event: node_started``."""

    def test_node_emit_lands_on_custom_channel_and_sse(self):
        from typing import TypedDict
        from consultants.engine.events import (
            NodeFinished, NodeStarted, emit,
        )
        from consultants.server.events_sse import (
            sse_from_astream_events,
        )

        class S(TypedDict, total=False):
            done: bool

        def my_node(state):
            emit(NodeStarted(role="researcher", lane_idx=1,
                              model="kimi"))
            emit(NodeFinished(role="researcher", lane_idx=1,
                               duration_ms=42, ok=True))
            return {"done": True}

        sg = StateGraph(S)
        sg.add_node("worker", my_node)
        sg.add_edge(START, "worker")
        sg.add_edge("worker", END)
        graph = sg.compile()

        async def run() -> list[bytes]:
            # Wrap astream_events as an async iterator the bridge
            # accepts.
            astream = graph.astream_events({"done": False},
                                            version="v2")
            chunks: list[bytes] = []
            async for chunk in sse_from_astream_events(
                    astream, sid="csl-test",
                    heartbeat_s=600.0,
            ):
                chunks.append(chunk)
            return chunks

        out = asyncio.run(run())
        # We expect at least two ``event:`` lines from our two
        # emits, plus possibly lifecycle frames from LangGraph.
        all_text = b"\n".join(out).decode("utf-8")
        self.assertIn("event: node_started", all_text)
        self.assertIn("event: node_finished", all_text)
        # And the SID was injected into the data dict by the
        # bridge (we passed sid="csl-test").
        self.assertIn('"sid": "csl-test"', all_text)

    def test_node_emit_preserves_fields(self):
        from typing import TypedDict
        from consultants.engine.events import NodeStarted, emit
        from consultants.server.events_sse import (
            sse_from_astream_events,
        )

        class S(TypedDict, total=False):
            done: bool

        def my_node(_state):
            emit(NodeStarted(role="critic", round=3,
                              lane_idx=7, model="glm-5.1:cloud"))
            return {"done": True}

        sg = StateGraph(S)
        sg.add_node("worker", my_node)
        sg.add_edge(START, "worker")
        sg.add_edge("worker", END)
        graph = sg.compile()

        async def run():
            astream = graph.astream_events({"done": False},
                                            version="v2")
            chunks: list[bytes] = []
            async for chunk in sse_from_astream_events(
                    astream, heartbeat_s=600.0,
            ):
                chunks.append(chunk)
            return chunks

        out = asyncio.run(run())
        # Find the node_started message and parse its data line.
        node_started = next(
            (c for c in out if b"event: node_started" in c),
            None,
        )
        self.assertIsNotNone(node_started)
        text = node_started.decode("utf-8")
        data_line = next(
            line for line in text.splitlines()
            if line.startswith("data:")
        )
        payload = json.loads(data_line[len("data: "):])
        self.assertEqual(payload["role"], "critic")
        self.assertEqual(payload["round"], 3)
        self.assertEqual(payload["lane_idx"], 7)
        self.assertEqual(payload["model"], "glm-5.1:cloud")


if __name__ == "__main__":
    unittest.main()
