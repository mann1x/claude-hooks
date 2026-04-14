"""
Extract metadata from request / response bodies without modifying them.

Called from the proxy handler to build the JSONL log record. Must be
resilient to:

- JSON parse failures (return partial info)
- SSE (``text/event-stream``) responses — we peek the first ``message_start``
  event for the ``model`` + ``usage.input_tokens`` (the final usage arrives
  in ``message_delta`` but that's not in the first chunk; P1 will tail
  the stream for full usage)
- Binary / non-JSON bodies (skip)
- The synthetic-rate-limit marker: ``"model": "<synthetic>"`` with
  zero usage counts — proves the CLI blocked the call locally without
  hitting the API (per ArkNill's B3 analysis of issue #42796)
"""

from __future__ import annotations

import json
from typing import Any, Optional


def extract_request_info(
    body: bytes,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Return ``{'model_requested', 'is_warmup', 'session_id'}`` from a JSON body."""
    out: dict[str, Any] = {
        "model_requested": None,
        "is_warmup": False,
        "session_id": None,
    }
    if not body:
        return out
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    out["model_requested"] = data.get("model")
    # Warmup detection: per our analysis of #17457 / #47922, warmup
    # subagent calls open with a user message whose only text block is
    # exactly "Warmup". Detect by the literal content.
    msgs = data.get("messages") or []
    if isinstance(msgs, list) and msgs:
        first = msgs[0]
        if isinstance(first, dict) and first.get("role") == "user":
            content = first.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        break
            if text.strip() == "Warmup":
                out["is_warmup"] = True
    # Session / agent identifiers ride along metadata.user_id or headers.
    md = data.get("metadata")
    if isinstance(md, dict):
        out["session_id"] = md.get("user_id") or md.get("session_id")
    return out


def extract_response_info(
    headers: dict[str, str],
    first_chunk: Optional[bytes],
) -> dict[str, Any]:
    """Return status / model / usage / rate-limit info from response headers +
    first body chunk. ``first_chunk`` may be the full body (JSON) or the
    opening SSE event bytes.
    """
    out: dict[str, Any] = {
        "model_delivered": None,
        "usage": None,
        "rate_limit": _extract_rate_limit(headers),
        "synthetic": False,
    }
    if not first_chunk:
        return out
    # Try JSON first.
    parsed = None
    try:
        parsed = json.loads(first_chunk)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        out["model_delivered"] = parsed.get("model")
        u = parsed.get("usage")
        if isinstance(u, dict):
            out["usage"] = u
        if parsed.get("model") == "<synthetic>":
            out["synthetic"] = True
        return out
    # Else try SSE: scan the chunk for the first ``data: {...}`` line of type
    # ``message_start``. That event carries the model + initial usage.
    try:
        text = first_chunk.decode("utf-8", errors="replace")
    except Exception:
        return out
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        if evt.get("type") == "message_start":
            msg = evt.get("message") or {}
            out["model_delivered"] = msg.get("model")
            u = msg.get("usage")
            if isinstance(u, dict):
                out["usage"] = u
            if msg.get("model") == "<synthetic>":
                out["synthetic"] = True
            break
    return out


# ------------------------------------------------------------------ #
# Rate-limit headers
# ------------------------------------------------------------------ #
# Anthropic returns a cluster of ``anthropic-ratelimit-unified-*``
# headers for subscription users (per ArkNill's analysis). Capture the
# numeric ones verbatim; leave the raw dict under ``rate_limit`` for
# P1 to consume.

_RL_HEADER_PREFIXES = (
    "anthropic-ratelimit-",
    "x-ratelimit-",   # older variant seen on some endpoints
    "retry-after",
)


def _extract_rate_limit(headers: dict[str, str]) -> Optional[dict[str, str]]:
    if not headers:
        return None
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        for prefix in _RL_HEADER_PREFIXES:
            if lk.startswith(prefix):
                out[lk] = v
                break
    return out or None
