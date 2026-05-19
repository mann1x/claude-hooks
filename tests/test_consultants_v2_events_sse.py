"""Tests for ``consultants.server.events_sse`` — SSE wire format,
``astream_events`` v2 demultiplexer, and the live + replay async
iterators.

Three test surfaces:

1. **Pure formatters** — ``format_sse_event`` + ``format_sse_heartbeat``.
2. **Demux** — ``classify_astream_event`` over synthetic dicts that
   mimic LangGraph's astream_events v2 shape.
3. **Async iterators** — ``sse_from_astream_events`` (live stream
   with heartbeats) + ``sse_replay_from_rows`` (Last-Event-ID
   resume). Both use asyncio.run to drive the iterators in a
   single-threaded event loop.

Pure-Python, no langgraph; the SSE bridge is intentionally
decoupled from the graph so it imports + tests cleanly in the
main ``claude-hooks`` env.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from typing import Any, AsyncIterator

from consultants.server.events_sse import (
    DEFAULT_HEARTBEAT_S,
    classify_astream_event,
    format_sse_event,
    format_sse_heartbeat,
    highest_event_id,
    sse_from_astream_events,
    sse_replay_from_rows,
)


# ============================================================== #
# 1. Wire formatters
# ============================================================== #

class TestFormatSseEvent(unittest.TestCase):

    def test_basic_event_shape(self):
        out = format_sse_event(
            event_id=1, event_type="node_started",
            data={"role": "researcher", "lane_idx": 2},
        )
        text = out.decode("utf-8")
        # Required fields land on their own lines.
        self.assertIn("id: 1", text)
        self.assertIn("event: node_started", text)
        self.assertIn('"role": "researcher"', text)
        # Trailing blank line per SSE spec.
        self.assertTrue(text.endswith("\n\n"))

    def test_event_with_retry(self):
        out = format_sse_event(
            event_id=1, event_type="hello", data={},
            retry_ms=3000,
        )
        text = out.decode("utf-8")
        self.assertIn("retry: 3000", text)

    def test_event_data_is_json_one_line(self):
        out = format_sse_event(
            event_id=42, event_type="custom",
            data={"key": "value", "n": 7},
        )
        text = out.decode("utf-8")
        # Find the data line and parse as JSON.
        data_line = [
            line for line in text.splitlines()
            if line.startswith("data:")
        ][0]
        payload = json.loads(data_line[len("data: "):])
        self.assertEqual(payload["key"], "value")
        self.assertEqual(payload["n"], 7)

    def test_event_type_required_non_empty(self):
        with self.assertRaises(ValueError):
            format_sse_event(event_id=1, event_type="", data={})

    def test_event_data_supports_non_ascii(self):
        out = format_sse_event(
            event_id=1, event_type="note",
            data={"text": "日本語 + emoji 🎉"},
        )
        text = out.decode("utf-8")
        self.assertIn("日本語", text)
        self.assertIn("🎉", text)

    def test_event_data_default_str_fallback(self):
        """``default=str`` lets us serialize values that don't have
        native JSON encodings (datetimes, custom classes). Smoke
        check with a non-serializable value to make sure the
        formatter doesn't crash mid-stream."""
        class Custom:
            def __str__(self):
                return "custom-repr"
        out = format_sse_event(
            event_id=1, event_type="x",
            data={"value": Custom()},
        )
        text = out.decode("utf-8")
        self.assertIn("custom-repr", text)


class TestFormatHeartbeat(unittest.TestCase):

    def test_heartbeat_is_sse_comment(self):
        out = format_sse_heartbeat()
        self.assertEqual(out, b": heartbeat\n\n")
        # Starts with `:` so SSE consumers ignore it.
        self.assertTrue(out.startswith(b":"))


# ============================================================== #
# 2. Demultiplexer
# ============================================================== #

class TestClassifyAstreamEvent(unittest.TestCase):

    def test_custom_event_uses_kind_as_type(self):
        raw = {
            "event": "on_custom_event",
            "name": "custom",
            "data": {"kind": "node_started", "role": "researcher"},
        }
        et, data, drop = classify_astream_event(raw)
        self.assertFalse(drop)
        self.assertEqual(et, "node_started")
        self.assertEqual(data["role"], "researcher")

    def test_custom_event_injects_sid_when_missing(self):
        raw = {"event": "on_custom_event",
                "data": {"kind": "x"}}
        et, data, _ = classify_astream_event(raw, sid="csl-abc")
        self.assertEqual(data["sid"], "csl-abc")

    def test_custom_event_keeps_existing_sid(self):
        raw = {"event": "on_custom_event",
                "data": {"kind": "x", "sid": "csl-foo"}}
        et, data, _ = classify_astream_event(raw, sid="csl-other")
        self.assertEqual(data["sid"], "csl-foo")

    def test_chat_model_stream_becomes_token(self):
        # The data carries an AIMessageChunk-shaped dict.
        raw = {
            "event": "on_chat_model_stream",
            "name": "researcher",
            "data": {"chunk": {"content": "Hello"}},
        }
        et, data, drop = classify_astream_event(raw)
        self.assertFalse(drop)
        self.assertEqual(et, "token")
        self.assertEqual(data["delta"], "Hello")
        self.assertEqual(data["role"], "researcher")

    def test_chat_model_stream_with_empty_chunk_dropped(self):
        raw = {"event": "on_chat_model_stream",
                "data": {"chunk": {"content": ""}}}
        _, _, drop = classify_astream_event(raw)
        self.assertTrue(drop)

    def test_chain_start_becomes_lifecycle_start(self):
        raw = {"event": "on_chain_start", "name": "researcher"}
        et, data, drop = classify_astream_event(raw)
        self.assertFalse(drop)
        self.assertEqual(et, "lifecycle")
        self.assertEqual(data["phase"], "start")
        self.assertEqual(data["name"], "researcher")

    def test_chain_end_becomes_lifecycle_end(self):
        raw = {"event": "on_chain_end", "name": "researcher"}
        _, data, _ = classify_astream_event(raw)
        self.assertEqual(data["phase"], "end")

    def test_other_events_dropped(self):
        raw = {"event": "on_chat_model_start", "name": "x"}
        _, _, drop = classify_astream_event(raw)
        self.assertTrue(drop)

    def test_non_dict_dropped(self):
        _, _, drop = classify_astream_event(None)  # type: ignore[arg-type]
        self.assertTrue(drop)


# ============================================================== #
# 3. Live SSE iterator
# ============================================================== #

async def _as_aiter(items: list[Any]) -> AsyncIterator[Any]:
    """Build an async iterator that yields ``items`` one at a time."""
    for it in items:
        yield it


async def _collect(aiter: AsyncIterator[bytes]) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in aiter:
        out.append(chunk)
    return out


class TestSseFromAstreamEvents(unittest.TestCase):

    def test_emits_events_in_order(self):
        events = [
            {"event": "on_custom_event",
             "data": {"kind": "node_started", "role": "planner"}},
            {"event": "on_custom_event",
             "data": {"kind": "node_finished", "role": "planner",
                       "ok": True}},
        ]
        async def run():
            return await _collect(sse_from_astream_events(
                _as_aiter(events),
                # Disable heartbeats for this test.
                heartbeat_s=600.0,
            ))
        out = asyncio.run(run())
        # Two SSE messages.
        self.assertEqual(len(out), 2)
        self.assertIn(b"event: node_started", out[0])
        self.assertIn(b"event: node_finished", out[1])
        # Monotonically increasing IDs starting at 1.
        self.assertIn(b"id: 1", out[0])
        self.assertIn(b"id: 2", out[1])

    def test_first_event_carries_retry_hint(self):
        events = [{"event": "on_custom_event",
                    "data": {"kind": "x"}}]
        async def run():
            return await _collect(sse_from_astream_events(
                _as_aiter(events),
                heartbeat_s=600.0, initial_retry_ms=5000,
            ))
        out = asyncio.run(run())
        self.assertIn(b"retry: 5000", out[0])

    def test_subsequent_events_dont_repeat_retry(self):
        events = [
            {"event": "on_custom_event", "data": {"kind": "a"}},
            {"event": "on_custom_event", "data": {"kind": "b"}},
        ]
        async def run():
            return await _collect(sse_from_astream_events(
                _as_aiter(events),
                heartbeat_s=600.0, initial_retry_ms=3000,
            ))
        out = asyncio.run(run())
        self.assertIn(b"retry: 3000", out[0])
        self.assertNotIn(b"retry:", out[1])

    def test_drops_uninteresting_events(self):
        events = [
            {"event": "on_chat_model_start", "name": "x"},  # drop
            {"event": "on_custom_event", "data": {"kind": "x"}},
            {"event": "on_llm_end", "name": "y"},  # drop
        ]
        async def run():
            return await _collect(sse_from_astream_events(
                _as_aiter(events), heartbeat_s=600.0,
            ))
        out = asyncio.run(run())
        self.assertEqual(len(out), 1)

    def test_start_event_id_offsets_numbering(self):
        events = [{"event": "on_custom_event", "data": {"kind": "x"}}]
        async def run():
            return await _collect(sse_from_astream_events(
                _as_aiter(events),
                start_event_id=42, heartbeat_s=600.0,
            ))
        out = asyncio.run(run())
        self.assertIn(b"id: 43", out[0])

    def test_heartbeat_fires_when_upstream_is_silent(self):
        """No upstream events → heartbeat after the deadline elapses.

        We use a real-time short heartbeat with an async sleeping
        iterator to keep the test fast.
        """
        async def slow_stream():
            # Sleep longer than the heartbeat interval, then emit
            # one real event and finish.
            await asyncio.sleep(0.15)
            yield {"event": "on_custom_event",
                    "data": {"kind": "ping"}}

        async def run():
            chunks = await _collect(sse_from_astream_events(
                slow_stream(), heartbeat_s=0.05,
            ))
            return chunks

        out = asyncio.run(run())
        # Expect at least one heartbeat (comment line) before the
        # event lands.
        heartbeats = [c for c in out if c.startswith(b": heartbeat")]
        events = [c for c in out if c.startswith(b"id:")]
        self.assertGreaterEqual(len(heartbeats), 1)
        self.assertEqual(len(events), 1)


# ============================================================== #
# 4. Replay iterator (Last-Event-ID resume)
# ============================================================== #

class TestSseReplayFromRows(unittest.TestCase):

    def test_replays_rows_in_order(self):
        rows = [
            {"event_id": 1, "ts": 1.0, "kind": "node_started",
             "payload": {"role": "planner"}},
            {"event_id": 2, "ts": 2.0, "kind": "node_finished",
             "payload": {"role": "planner", "ok": True}},
        ]
        async def run():
            return await _collect(sse_replay_from_rows(rows))
        out = asyncio.run(run())
        self.assertEqual(len(out), 2)
        self.assertIn(b"event: node_started", out[0])
        self.assertIn(b"event: node_finished", out[1])
        self.assertIn(b"id: 1", out[0])
        self.assertIn(b"id: 2", out[1])

    def test_since_event_id_skips_already_seen(self):
        rows = [
            {"event_id": 1, "ts": 1.0, "kind": "a",
             "payload": {"i": 1}},
            {"event_id": 2, "ts": 2.0, "kind": "b",
             "payload": {"i": 2}},
            {"event_id": 3, "ts": 3.0, "kind": "c",
             "payload": {"i": 3}},
        ]
        async def run():
            return await _collect(sse_replay_from_rows(
                rows, start_event_id=1,
            ))
        out = asyncio.run(run())
        # Rows 2 and 3 only.
        self.assertEqual(len(out), 2)
        self.assertIn(b"id: 2", out[0])
        self.assertIn(b"id: 3", out[1])

    def test_empty_rows_yields_nothing(self):
        async def run():
            return await _collect(sse_replay_from_rows([]))
        self.assertEqual(asyncio.run(run()), [])


class TestHighestEventId(unittest.TestCase):

    def test_finds_max(self):
        rows = [{"event_id": 5}, {"event_id": 2}, {"event_id": 12}]
        self.assertEqual(highest_event_id(rows), 12)

    def test_empty_returns_zero(self):
        self.assertEqual(highest_event_id([]), 0)

    def test_missing_event_id_treated_as_zero(self):
        rows = [{"kind": "x"}, {"event_id": 3}]
        self.assertEqual(highest_event_id(rows), 3)


if __name__ == "__main__":
    unittest.main()
