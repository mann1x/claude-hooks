"""
Stub responder for blocked Warmup calls (Phase P3).

When ``proxy.block_warmup`` is true and a request opens with the
``"Warmup"`` prompt, we short-circuit without touching the upstream —
returning a minimal but spec-compliant Anthropic message.

The stub must satisfy whatever Claude Code does with Warmup responses
(quick token sanity check, cache priming). In practice it only needs:

- ``type: "message"``, ``role: "assistant"``
- ``model`` echoing the request
- ``content: [{"type": "text", "text": ""}]`` — empty text
- ``stop_reason: "end_turn"``
- ``usage`` with zeros

Covers both streaming and non-streaming shapes:

- Non-streaming: a JSON body
- Streaming (``"stream": true``): SSE events
  ``message_start`` → ``content_block_start`` → ``content_block_delta``
  → ``content_block_stop`` → ``message_delta`` → ``message_stop``
"""

from __future__ import annotations

import json
from typing import Optional


_HEADERS_NON_STREAM = {
    "Content-Type": "application/json",
    "X-Claude-Hooks-Proxy": "warmup-blocked",
}

_HEADERS_STREAM = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "X-Claude-Hooks-Proxy": "warmup-blocked",
}


def _empty_usage() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def build_non_streaming(model: Optional[str], message_id: str) -> tuple[int, dict, bytes]:
    """Return ``(status, headers, body)`` for a non-streaming stub."""
    payload = {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model or "claude-haiku-4-5",
        "content": [{"type": "text", "text": ""}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": _empty_usage(),
    }
    body = json.dumps(payload).encode("utf-8")
    headers = dict(_HEADERS_NON_STREAM)
    headers["Content-Length"] = str(len(body))
    return 200, headers, body


def build_streaming(model: Optional[str], message_id: str) -> tuple[int, dict, bytes]:
    """Return ``(status, headers, body_bytes)`` for an SSE stub.

    All events are packed into one bytes blob since the whole stub is
    tiny and there's no back-pressure to worry about.
    """
    m = model or "claude-haiku-4-5"

    def _event(name: str, payload: dict) -> bytes:
        return (
            f"event: {name}\n"
            f"data: {json.dumps(payload)}\n\n"
        ).encode("utf-8")

    parts: list[bytes] = []
    parts.append(_event("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": m,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": _empty_usage(),
        },
    }))
    parts.append(_event("content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }))
    parts.append(_event("content_block_delta", {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": ""},
    }))
    parts.append(_event("content_block_stop", {
        "type": "content_block_stop",
        "index": 0,
    }))
    parts.append(_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 0},
    }))
    parts.append(_event("message_stop", {
        "type": "message_stop",
    }))
    body = b"".join(parts)
    headers = dict(_HEADERS_STREAM)
    # SSE can also be sent with an explicit Content-Length; simpler than
    # chunked for this tiny payload and reliably consumed by http.client.
    headers["Content-Length"] = str(len(body))
    return 200, headers, body
