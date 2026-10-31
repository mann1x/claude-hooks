"""Where a chat request runs on a PolyKV engine, on each wire (plan F1).

The council asks for a placement by putting one internal key on the
payload it hands a chat client::

    payload["placement"] = {
        "session_id": "<sid>~r2",      # the agent's own engine session
        "pool_id": "3",                # attach (a worker of the pool's owner)
        "continue_pool": False,        # the pool's tokens + the new turns
        "num_ctx": 65536,              # book a window (owners only)
        "num_ctx_min": 32768,          # the floor of that booking
        "keepalive": True,             # open SSE at once, keepalive comments
        "return_progress": True,       # prefill progress frames (liveness)
        "chat_template_kwargs": {...}, # must match the pool's render
    }

A payload without it is sent byte-for-byte as before: both clients build
their body from an explicit list of keys, and only :func:`to_openai` /
:func:`to_ollama` read this one.

- **opencoti direct** (``/v1/chat/completions``): top-level ``session_id``,
  ``pool_id`` (an int on the wire; 0 is a pool), ``continue_pool``,
  ``num_ctx`` / ``num_ctx_min``, ``cache_prompt``, ``chat_template_kwargs``;
  on a stream ``stream_options.keepalive`` and ``return_progress``.
- **xOllama** (native ``/api/chat`` only): top-level ``session_id`` and
  ``placement{pool_id, num_ctx, num_ctx_min}``, never ``options.num_ctx``
  for the booking (that sizes the model load). xOllama carries no
  ``continue_pool``; a request for it is dropped with a warning.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("claude_hooks.polykv")

KEY = "placement"


def placement_of(payload: Optional[dict]) -> Optional[dict]:
    p = (payload or {}).get(KEY)
    return p if isinstance(p, dict) and p else None


def _pool_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def to_openai(body: dict, placement: Optional[dict], *, stream: bool) -> dict:
    """Add the placement to an opencoti ``/v1/chat/completions`` body."""
    if not placement:
        return body
    sid = placement.get("session_id")
    if sid:
        body["session_id"] = str(sid)
        body["cache_prompt"] = True
    pool = _pool_int(placement.get("pool_id"))
    if pool is not None:
        body["pool_id"] = pool
        if placement.get("continue_pool"):
            body["continue_pool"] = True
    elif placement.get("continue_pool"):
        raise ValueError("continue_pool needs a pool_id")
    for key in ("num_ctx", "num_ctx_min"):
        if placement.get(key):
            body[key] = int(placement[key])
    if placement.get("chat_template_kwargs"):
        body["chat_template_kwargs"] = dict(placement["chat_template_kwargs"])
    if stream:
        so = dict(body.get("stream_options") or {})
        if placement.get("keepalive"):
            so["keepalive"] = True
        if so:
            body["stream_options"] = so
        if placement.get("return_progress"):
            body["return_progress"] = True
    return body


def to_ollama(body: dict, placement: Optional[dict]) -> dict:
    """Add the placement to an xOllama native ``/api/chat`` body."""
    if not placement:
        return body
    sid = placement.get("session_id")
    if sid:
        body["session_id"] = str(sid)
    place: dict = {}
    pool = _pool_int(placement.get("pool_id"))
    if pool is not None:
        place["pool_id"] = pool
    for key in ("num_ctx", "num_ctx_min"):
        if placement.get(key):
            place[key] = int(placement[key])
    if place:
        body["placement"] = place
    if placement.get("continue_pool"):
        log.warning("polykv: xOllama /api/chat carries no continue_pool; "
                    "sent as a plain attach to pool %s", pool)
    return body


def observation_record(obs, placement: dict) -> dict:
    """The run record's ``timings["polykv"]``: what was asked, what the
    engine says happened."""
    out: dict = {"session_id": placement.get("session_id")}
    for key in ("pool_id", "boot_id", "context_window", "n_pool_shared",
                "pool_match", "pool_len", "cache_n", "sessions_remaining"):
        val = getattr(obs, key)
        if val is not None:
            out[key] = val
    if obs.pool_unknown:
        out["pool_unknown"] = True
    if obs.warnings:
        out["warnings"] = list(obs.warnings)
    return out


__all__ = ["KEY", "observation_record", "placement_of", "to_ollama",
           "to_openai"]
