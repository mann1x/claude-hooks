"""
Non-buffering SSE tailer.

Wraps a byte-stream iterator and parses Anthropic's SSE events as they
flow past, updating a mutable ``usage_accumulator`` dict without ever
modifying the bytes going to the client.

The two events we care about:

- ``message_start`` — carries the initial ``message.usage`` (input
  tokens, cache tokens). We already get this in the first 4 KB and
  metadata.extract_response_info handles it. This module focuses on
  the trailing events.
- ``message_delta`` — carries the final ``usage`` block with
  ``output_tokens`` (the prompt caching + cache-creation counts also
  settle here). This is the canonical billing number.

Contract:

    tail = SseTail()
    for chunk in tail.wrap(body_iter):
        client.write(chunk)     # bytes are passed through verbatim
    # After the loop:
    tail.final_usage  -> {'input_tokens': ..., 'output_tokens': ..., ...}
    tail.stop_reason  -> 'end_turn' | 'tool_use' | 'max_tokens' | ...
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Iterator, Optional

log = logging.getLogger("claude_hooks.proxy.sse")


class SseTail:
    """Incrementally parse SSE events without buffering the whole stream."""

    def __init__(self) -> None:
        self._buffer = b""
        self.final_usage: Optional[dict] = None
        self.stop_reason: Optional[str] = None
        self.event_counts: dict[str, int] = {}
        # S3 — thinking-block metrics.
        #
        # stellaraccident's analysis of issue #42796 showed the SSE
        # ``signature`` field on thinking blocks has a 0.971 Pearson
        # correlation with thinking content length. Even when the
        # ``redact-thinking-*`` beta hides the text, signature-byte
        # length still lets us estimate reasoning depth.
        #
        # ``thinking_delta_count``   — count of thinking_delta events
        # ``thinking_signature_bytes`` — sum of signature-field byte
        #   lengths across all thinking content blocks in this stream
        # ``thinking_output_tokens`` — populated if Anthropic ever
        #   breaks thinking out in usage (stellaraccident's explicit
        #   ask in #42796); seeded from message_delta.usage if a
        #   ``thinking_output_tokens`` key appears there.
        self.thinking_delta_count: int = 0
        self.thinking_signature_bytes: int = 0
        self.thinking_output_tokens: Optional[int] = None
        # Diagnostic: track content_block types + delta types seen.
        # Lets us learn the actual wire vocabulary (e.g. whether
        # "extended_thinking" or "redacted_thinking" appears instead
        # of plain "thinking") without dumping response bodies.
        self.content_block_types: dict[str, int] = {}
        self.delta_types: dict[str, int] = {}

    def wrap(self, body_iter: Iterable[bytes]) -> Iterator[bytes]:
        """Yield chunks verbatim while parsing SSE events side-effect-only."""
        for chunk in body_iter:
            if chunk:
                self._feed(chunk)
            yield chunk
        # Flush any trailing partial event (rare — SSE frames end in \n\n).
        if self._buffer:
            self._parse_event(self._buffer)
            self._buffer = b""

    def wrap_bytes(self, initial: bytes, rest: Iterable[bytes]) -> Iterator[bytes]:
        """Convenience: feed an ``initial`` chunk + the rest. Preserves order."""
        if initial:
            self._feed(initial)
            yield initial
        yield from self.wrap(rest)

    # --------------------------------------------------------------- #
    def _feed(self, data: bytes) -> None:
        self._buffer += data
        # SSE events are separated by blank lines (\n\n or \r\n\r\n).
        while True:
            sep_idx = -1
            for sep in (b"\n\n", b"\r\n\r\n"):
                idx = self._buffer.find(sep)
                if idx != -1 and (sep_idx == -1 or idx < sep_idx):
                    sep_idx = idx
                    sep_len = len(sep)
            if sep_idx == -1:
                return
            event = self._buffer[:sep_idx]
            self._buffer = self._buffer[sep_idx + sep_len :]
            self._parse_event(event)

    def _parse_event(self, event_bytes: bytes) -> None:
        if not event_bytes:
            return
        event_type: Optional[str] = None
        data_parts: list[str] = []
        try:
            text = event_bytes.decode("utf-8", errors="replace")
        except Exception:
            return
        for line in text.splitlines():
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_parts.append(line[5:].lstrip())
        if not data_parts:
            return
        raw = "\n".join(data_parts)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return

        # Some servers emit the type only inside the data payload.
        etype = event_type or payload.get("type")
        if not etype:
            return
        self.event_counts[etype] = self.event_counts.get(etype, 0) + 1

        if etype == "message_delta":
            usage = payload.get("usage")
            if isinstance(usage, dict):
                # message_delta's usage is a *delta* on output_tokens per the
                # Anthropic spec. We overwrite with the most recent value;
                # the last message_delta before message_stop carries the
                # final total.
                self.final_usage = dict(usage)
                # If Anthropic ever ships thinking_output_tokens in the
                # usage block (stellaraccident's ask), capture it.
                tot = usage.get("thinking_output_tokens")
                if isinstance(tot, int):
                    self.thinking_output_tokens = tot
            delta = payload.get("delta")
            if isinstance(delta, dict):
                sr = delta.get("stop_reason")
                if sr:
                    self.stop_reason = sr
        elif etype == "message_start":
            msg = payload.get("message") or {}
            usage = msg.get("usage")
            if isinstance(usage, dict) and self.final_usage is None:
                # Seed from message_start if message_delta hasn't arrived
                # yet — gives us input_tokens / cache_* which message_delta
                # doesn't repeat.
                self.final_usage = dict(usage)
        elif etype == "content_block_start":
            block = payload.get("content_block") or {}
            if isinstance(block, dict):
                bt = block.get("type") or "<unknown>"
                self.content_block_types[bt] = self.content_block_types.get(bt, 0) + 1
                # S3: thinking blocks expose a ``signature`` field
                # whose byte length correlates with thinking content
                # length even when the text is redacted (0.971 Pearson
                # per #42796). Match any "thinking" variant
                # (``thinking``, ``extended_thinking``,
                # ``redacted_thinking``) since the exact label has
                # evolved between API versions.
                if "thinking" in bt:
                    sig = block.get("signature")
                    if isinstance(sig, (str, bytes)):
                        self.thinking_signature_bytes += len(sig)
        elif etype == "content_block_delta":
            delta = payload.get("delta") or {}
            if isinstance(delta, dict):
                dt = delta.get("type") or "<unknown>"
                self.delta_types[dt] = self.delta_types.get(dt, 0) + 1
                # The thinking stream on Claude Opus 4.6 actually
                # arrives as ``signature_delta`` events (observed live
                # 2026-04-15). Accumulate the signature bytes and
                # count events as thinking-deltas regardless of label.
                # ``thinking_delta`` remains a valid variant for
                # non-redacted responses that carry text chunks.
                if dt == "signature_delta" or "thinking" in dt:
                    self.thinking_delta_count += 1
                    sig = delta.get("signature")
                    if isinstance(sig, str):
                        self.thinking_signature_bytes += len(sig)
        elif etype == "message_stop":
            # Nothing more to parse — the stream is done.
            pass


def merge_usage(
    start: Optional[dict], delta: Optional[dict]
) -> Optional[dict]:
    """Merge message_start usage (input/cache) with message_delta usage
    (output/cache_creation). Returns a single dict with all known counters.
    """
    if not start and not delta:
        return None
    out: dict[str, Any] = {}
    if isinstance(start, dict):
        out.update(start)
    if isinstance(delta, dict):
        out.update(delta)
    return out
