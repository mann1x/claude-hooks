"""Server-Sent Events bridge over LangGraph's ``astream_events``
v2 stream.

The M9 ``GET /v1/consult/<sid>/events`` endpoint wraps the iterator
from :func:`sse_from_astream_events` into a FastAPI
``StreamingResponse(..., media_type="text/event-stream")``.

The module is split into three layers so each is independently
testable:

1. **Pure SSE formatters** — :func:`format_sse_event`,
   :func:`format_sse_heartbeat`. Convert a Python dict (or
   nothing, for heartbeats) into the wire-format bytes a browser /
   SSE client expects. No I/O, no async.

2. **Demultiplexer** — :func:`classify_astream_event`. Pure
   function over one ``astream_events`` v2 payload that returns
   the ``(event_type, event_data, drop)`` triple the formatter
   should emit. Lets us bake the demux logic in tests without
   spinning up a real graph.

3. **Async iterators** — :func:`sse_from_astream_events` (live
   stream + periodic heartbeats) and :func:`sse_replay_from_rows`
   (Last-Event-ID resume from the recorder's ``runtime_events``
   table). Both yield ``bytes`` ready to write to the response.

Event-type mapping (plan §M4 event taxonomy + LangGraph 1.2 docs):

- ``on_custom_event`` (our ``emit(event)`` calls)
  → ``event: <event.kind>`` (e.g. ``event: node_started``)
- ``on_chat_model_stream`` (per-token deltas from chat models)
  → ``event: token``
- ``on_chain_start`` / ``on_chain_end``
  → ``event: lifecycle``
- everything else
  → dropped (consumers don't need the metadata-only events)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Callable, Iterable, Optional

log = logging.getLogger("consultants.server.events_sse")


# Default cadence the bridge nudges out a heartbeat when there's
# no real traffic. Below ~30s, proxies sitting in front of the
# app (nginx, traefik) sometimes close the connection thinking the
# client died. 15 s is the common-practice middle ground.
DEFAULT_HEARTBEAT_S: float = 15.0


# ============================================================== #
# 1. SSE wire formatters
# ============================================================== #

def format_sse_event(
    *,
    event_id: int,
    event_type: str,
    data: dict[str, Any],
    retry_ms: Optional[int] = None,
) -> bytes:
    """Format one SSE message in wire format.

    Wire example::

        id: 42
        event: node_started
        retry: 3000
        data: {"kind":"node_started","role":"researcher",...}

        ← trailing blank line is REQUIRED; consumers parse on it

    ``data`` is JSON-encoded with ``ensure_ascii=False`` so non-
    ASCII characters survive untransformed. Multi-line JSON would
    require ``data:`` prefix per line; we emit one-line JSON for
    simplicity (works for all our payload shapes).

    ``retry_ms`` is optional — when present the consumer reconnects
    with that backoff. Mostly useful on the first event of a long
    stream.
    """
    if not event_type or not isinstance(event_type, str):
        raise ValueError("event_type must be a non-empty string")
    parts: list[str] = []
    parts.append(f"id: {int(event_id)}")
    parts.append(f"event: {event_type}")
    if retry_ms is not None:
        parts.append(f"retry: {int(retry_ms)}")
    parts.append(
        "data: " + json.dumps(data, ensure_ascii=False, default=str)
    )
    # Trailing blank line per the SSE spec.
    parts.append("")
    parts.append("")
    return "\n".join(parts).encode("utf-8")


def format_sse_heartbeat() -> bytes:
    """Format an SSE comment-line heartbeat.

    Wire format::

        : heartbeat
        ←blank line

    Comment lines start with `:` and are ignored by SSE consumers
    but keep proxies / load balancers from closing idle
    connections.
    """
    return b": heartbeat\n\n"


# ============================================================== #
# 2. astream_events demultiplexer (pure)
# ============================================================== #

# Return shape: (event_type, data_dict, drop_flag)
# When drop_flag is True the consumer skips this event entirely.
_DropSignal = tuple[str, dict[str, Any], bool]


def classify_astream_event(
    raw: dict[str, Any],
    *,
    sid: Optional[str] = None,
) -> _DropSignal:
    """Map one ``astream_events`` v2 record to an SSE event type +
    data dict, or signal it should be dropped.

    LangGraph 1.2 ``astream_events(version="v2")`` emits records
    with this top-level shape::

        {
            "event": "on_chain_start" | "on_chain_end" |
                     "on_chat_model_stream" |
                     "on_custom_event" | ...,
            "name":  "<node-or-emitter name>",
            "data":  {...},
            "run_id": "...",
            "tags":  [...],
            "metadata": {...},
        }

    We map:

    - ``on_custom_event`` → ``data["kind"]`` becomes the SSE event
      type; the full ``data`` dict becomes the SSE data payload
      (with ``sid`` patched in if supplied).
    - ``on_chat_model_stream`` → SSE event ``token`` with
      ``{"role": <name>, "delta": <content>}``.
    - ``on_chain_start`` / ``on_chain_end`` → SSE event
      ``lifecycle`` with ``{"event": event_name, "name": node_name}``.
    - Everything else → drop.

    Pure function over the input dict; no I/O. Safe to call from
    sync or async context.
    """
    if not isinstance(raw, dict):
        return ("drop", {}, True)
    et = raw.get("event")
    if et == "on_custom_event":
        data = dict(raw.get("data") or {})
        kind = data.get("kind") or "custom"
        if sid is not None and "sid" not in data:
            data["sid"] = sid
        return (str(kind), data, False)
    if et == "on_chat_model_stream":
        chunk = raw.get("data") or {}
        # ``chunk`` is typically a dict like {"chunk": <AIMessageChunk>}.
        # Pull the textual content if available; otherwise skip.
        msg_chunk = chunk.get("chunk") if isinstance(chunk, dict) else None
        text = ""
        if msg_chunk is not None:
            # AIMessageChunk has ``.content`` attribute; dicts have
            # "content" key. Handle both for cross-version safety.
            text = getattr(msg_chunk, "content", None)
            if text is None and isinstance(msg_chunk, dict):
                text = msg_chunk.get("content")
        if not text:
            return ("drop", {}, True)
        return ("token", {
            "role": raw.get("name") or "",
            "delta": text,
            **({"sid": sid} if sid else {}),
        }, False)
    if et in ("on_chain_start", "on_chain_end"):
        return ("lifecycle", {
            "phase": "start" if et.endswith("_start") else "end",
            "name": raw.get("name") or "",
            **({"sid": sid} if sid else {}),
        }, False)
    return ("drop", {}, True)


# ============================================================== #
# 3. Async iterators
# ============================================================== #

async def sse_from_astream_events(
    astream_iter: AsyncIterator[dict[str, Any]],
    *,
    sid: Optional[str] = None,
    heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    start_event_id: int = 0,
    initial_retry_ms: Optional[int] = 3000,
    classifier: Callable[..., _DropSignal] = classify_astream_event,
    time_source: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], Any] = asyncio.sleep,
) -> AsyncIterator[bytes]:
    """Convert a LangGraph ``astream_events(version="v2")`` iterator
    into an SSE byte stream with periodic heartbeats.

    Loops over the upstream stream concurrently with a heartbeat
    timer; whichever fires first wins. Per-event:

    1. Classify with :func:`classify_astream_event` (or the
       caller-supplied alternative for tests).
    2. If not dropped, format with :func:`format_sse_event` and
       yield. Increment the event ID counter.
    3. After yielding, reset the heartbeat deadline.

    On cancellation (client disconnect) the consumer's
    ``AsyncIterator.__anext__`` raises ``CancelledError``;
    propagating it cleanly is the caller's job (FastAPI's
    StreamingResponse handles it).

    ``start_event_id`` lets callers resume after a Last-Event-ID
    replay — the first live event gets ``start_event_id + 1``.
    """
    event_id = int(start_event_id)
    last_emit = time_source()
    retry_sent = False

    # Iterator-of-iterators trick: keep an in-flight ``__anext__``
    # task alive across heartbeats by racing it with ``asyncio.wait``
    # rather than ``wait_for``. ``wait_for`` cancels the inner
    # coroutine on timeout, which destroys the async-generator
    # source we're consuming; ``wait`` just times out without
    # touching the pending task, so the next iteration can pick up
    # right where we left off.
    it = astream_iter.__aiter__()
    pending_task: Optional[asyncio.Task[Any]] = None
    try:
        while True:
            if pending_task is None:
                pending_task = asyncio.ensure_future(it.__anext__())
            # Compute how long until the next heartbeat is due.
            elapsed = time_source() - last_emit
            deadline = max(0.0, heartbeat_s - elapsed)
            done, _ = await asyncio.wait(
                {pending_task}, timeout=deadline,
            )
            if pending_task not in done:
                # Timeout fired before upstream produced an event.
                yield format_sse_heartbeat()
                last_emit = time_source()
                continue
            # The pending task resolved — pick up its result and
            # clear the slot so the next iteration creates a fresh
            # __anext__ task.
            current_task = pending_task
            pending_task = None
            try:
                raw = current_task.result()
            except StopAsyncIteration:
                return
            event_type, data, drop = classifier(raw, sid=sid)
            if drop:
                continue
            event_id += 1
            retry = None
            if not retry_sent:
                retry = initial_retry_ms
                retry_sent = True
            yield format_sse_event(
                event_id=event_id, event_type=event_type,
                data=data, retry_ms=retry,
            )
            last_emit = time_source()
    finally:
        # If the consumer disconnected mid-stream we may still hold
        # a pending __anext__ task. Cancel it so the underlying
        # generator can clean up its frames (close any open files,
        # release locks, etc.).
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()


async def sse_replay_from_rows(
    rows: Iterable[dict[str, Any]],
    *,
    start_event_id: int = 0,
) -> AsyncIterator[bytes]:
    """Format a sequence of ``runtime_events`` rows (as returned by
    :py:meth:`MessageRecorder.list_runtime_events`) into SSE bytes.

    Used by the M9 endpoint for the ``Last-Event-ID`` resume case:
    the consumer sends ``Last-Event-ID: 42`` and we replay every
    row with ``event_id > 42`` from the recorder before resuming
    the live ``astream_events`` stream.

    Each row's ``event_id`` is used as the SSE id field — this
    keeps numbering monotonic across replay + live (the caller
    passes the highest replayed id as ``start_event_id`` into
    :func:`sse_from_astream_events`).

    ``start_event_id`` is a minimum threshold — rows with
    ``event_id <= start_event_id`` are skipped silently. Caller
    typically passes the consumer's Last-Event-ID directly.
    """
    for row in rows:
        eid = int(row.get("event_id") or 0)
        if eid <= start_event_id:
            continue
        kind = row.get("kind") or "custom"
        payload = dict(row.get("payload") or {})
        # Backfill sid if the row stored it; otherwise leave alone.
        yield format_sse_event(
            event_id=eid, event_type=str(kind), data=payload,
        )


def highest_event_id(rows: Iterable[dict[str, Any]]) -> int:
    """Cheap helper: max event_id across the rows, 0 if empty.

    Callers use this to know what to pass as ``start_event_id``
    to :func:`sse_from_astream_events` after a replay.
    """
    out = 0
    for row in rows:
        eid = int(row.get("event_id") or 0)
        if eid > out:
            out = eid
    return out


__all__ = [
    "DEFAULT_HEARTBEAT_S",
    "format_sse_event",
    "format_sse_heartbeat",
    "classify_astream_event",
    "sse_from_astream_events",
    "sse_replay_from_rows",
    "highest_event_id",
]
