"""
S3 tests — SseTail captures thinking-block metrics.

Based on stellaraccident's #42796 analysis: the ``signature`` field
on thinking blocks correlates 0.971 with thinking content length, so
summed signature bytes + delta counts give us a depth proxy even
under ``redact-thinking-*``.
"""

from __future__ import annotations

import json

from claude_hooks.proxy.sse import SseTail


def _sse(events: list[tuple[str, dict]]) -> bytes:
    """Serialise a list of ``(event_name, payload_dict)`` as SSE bytes."""
    out = []
    for name, payload in events:
        out.append(f"event: {name}\ndata: {json.dumps(payload)}\n\n")
    return "".join(out).encode()


def _inline_type(event_name: str, payload: dict) -> dict:
    """Ensure the payload's ``type`` matches the event name — some
    clients rely on ``data.type`` instead of the ``event:`` line.
    """
    payload = dict(payload)
    payload.setdefault("type", event_name)
    return payload


# ============================================================ #
class TestThinkingBlockParsing:
    def test_thinking_block_signature_and_deltas(self):
        sig = "a" * 128
        events = [
            ("message_start", {
                "type": "message_start",
                "message": {"model": "claude-opus-4-6",
                            "usage": {"input_tokens": 100, "output_tokens": 0}},
            }),
            ("content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "signature": sig},
            }),
            ("content_block_delta", _inline_type("content_block_delta", {
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "step 1"},
            })),
            ("content_block_delta", _inline_type("content_block_delta", {
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "step 2"},
            })),
            ("content_block_delta", _inline_type("content_block_delta", {
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "step 3"},
            })),
            ("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 200},
            }),
            ("message_stop", {"type": "message_stop"}),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_delta_count == 3
        assert tail.thinking_signature_bytes == 128
        assert tail.stop_reason == "end_turn"
        assert tail.final_usage is not None
        assert tail.final_usage.get("output_tokens") == 200

    def test_multiple_thinking_blocks_accumulate_signature(self):
        events = [
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "thinking", "signature": "x" * 50},
            }),
            ("content_block_start", {
                "type": "content_block_start", "index": 1,
                "content_block": {"type": "thinking", "signature": "y" * 70},
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_signature_bytes == 120
        assert tail.thinking_delta_count == 0  # no deltas in this mix

    def test_non_thinking_content_blocks_ignored(self):
        """``text`` / ``tool_use`` blocks must not count toward
        thinking metrics.
        """
        events = [
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text"},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            }),
            ("content_block_start", {
                "type": "content_block_start", "index": 1,
                "content_block": {"type": "tool_use", "name": "Edit"},
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_delta_count == 0
        assert tail.thinking_signature_bytes == 0

    def test_redacted_thinking_still_counts_signature(self):
        """Under ``redact-thinking-*`` the delta ``thinking`` field is
        empty or absent, but ``signature`` is still present on
        content_block_start — our depth proxy still works.
        """
        events = [
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "thinking", "signature": "z" * 256},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta"},   # no thinking text
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_signature_bytes == 256
        assert tail.thinking_delta_count == 1

    def test_thinking_output_tokens_captured_if_present(self):
        """If Anthropic ever surfaces thinking_output_tokens in the
        usage block (stellaraccident's ask), we record it.
        """
        events = [
            ("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 500,
                          "thinking_output_tokens": 200},
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_output_tokens == 200

    def test_real_anthropic_signature_delta_stream(self):
        """Claude Opus 4.6 streams thinking via ``signature_delta``
        events whose ``signature`` field accumulates base64 bytes —
        observed live on 2026-04-15. We count those and treat them as
        thinking-deltas for depth estimation.
        """
        events = [
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "thinking", "signature": ""},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "signature_delta",
                          "signature": "abcd1234" * 32},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "signature_delta",
                          "signature": "efgh5678" * 16},
            }),
            ("content_block_stop",
             {"type": "content_block_stop", "index": 0}),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_delta_count == 2
        assert tail.thinking_signature_bytes == (8 * 32) + (8 * 16)
        assert tail.content_block_types.get("thinking") == 1
        assert tail.delta_types.get("signature_delta") == 2

    def test_no_thinking_leaves_metrics_at_zero(self):
        events = [
            ("message_start", {
                "type": "message_start",
                "message": {"model": "claude-opus-4-6",
                            "usage": {"input_tokens": 10, "output_tokens": 0}},
            }),
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text"},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "hi"},
            }),
            ("message_stop", {"type": "message_stop"}),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_delta_count == 0
        assert tail.thinking_signature_bytes == 0
        assert tail.thinking_output_tokens is None


# ============================================================ #
# S4 — visible/redacted thinking split + tool-use counting
# ============================================================ #
class TestThinkingVisibleRedactedSplit:
    def test_signature_delta_counts_as_redacted(self):
        events = [
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "thinking", "signature": ""},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "signature_delta", "signature": "x" * 100},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "signature_delta", "signature": "y" * 100},
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_redacted_delta_count == 2
        assert tail.thinking_visible_delta_count == 0
        assert tail.thinking_delta_count == 2

    def test_thinking_delta_with_text_counts_as_visible(self):
        events = [
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "thinking"},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta",
                          "thinking": "Let me think step by step…"},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta",
                          "thinking": "The answer is 42."},
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_visible_delta_count == 2
        assert tail.thinking_redacted_delta_count == 0

    def test_mixed_visible_and_redacted(self):
        """Same stream can carry both — e.g. some chunks redacted and
        later chunks visible. Count each type independently.
        """
        events = [
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "signature_delta", "signature": "a" * 50},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta",
                          "thinking": "visible text"},
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_redacted_delta_count == 1
        assert tail.thinking_visible_delta_count == 1
        assert tail.thinking_delta_count == 2

    def test_empty_thinking_delta_not_counted_as_visible(self):
        """``thinking_delta`` events with empty/missing text shouldn't
        falsely inflate the visible count.
        """
        events = [
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta"},
            }),
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta", "thinking": ""},
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.thinking_visible_delta_count == 0
        # They still count toward the aggregate though.
        assert tail.thinking_delta_count == 2


class TestToolUseCounts:
    def test_single_tool_call(self):
        events = [
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "name": "Read"},
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.tool_use_counts == {"Read": 1}

    def test_multiple_tools_in_one_response(self):
        events = [
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "name": "Read"},
            }),
            ("content_block_start", {
                "type": "content_block_start", "index": 1,
                "content_block": {"type": "tool_use", "name": "Read"},
            }),
            ("content_block_start", {
                "type": "content_block_start", "index": 2,
                "content_block": {"type": "tool_use", "name": "Edit"},
            }),
            ("content_block_start", {
                "type": "content_block_start", "index": 3,
                "content_block": {"type": "tool_use", "name": "Bash"},
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.tool_use_counts == {"Read": 2, "Edit": 1, "Bash": 1}

    def test_thinking_and_text_blocks_dont_affect_tool_counts(self):
        events = [
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "thinking", "signature": ""},
            }),
            ("content_block_start", {
                "type": "content_block_start", "index": 1,
                "content_block": {"type": "text"},
            }),
            ("content_block_start", {
                "type": "content_block_start", "index": 2,
                "content_block": {"type": "tool_use", "name": "Grep"},
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.tool_use_counts == {"Grep": 1}

    def test_missing_name_recorded_as_unknown(self):
        events = [
            ("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use"},   # no name
            }),
        ]
        tail = SseTail()
        list(tail.wrap([_sse(events)]))
        assert tail.tool_use_counts == {"<unknown>": 1}
