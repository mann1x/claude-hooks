"""Typed event taxonomy for the v2 council + an ``emit()`` helper
that bridges to LangGraph's custom-event stream.

Why a dedicated module:

- The recorder already writes the bulk LLM / tool calls into its
  ``events`` table; that's the *artifact* view. This module owns
  the *streaming* view — events with a deterministic shape that
  the SSE bridge serializes and the M9 ``GET /events`` endpoint
  hands back to Claude / a human watching the council run.
- LangGraph 1.2 surfaces custom events on the ``"custom"`` channel
  of ``astream_events(version="v2")``. A node calls ``emit(event)``
  and the runtime wraps the payload with provenance (node name,
  thread id, run id) before delivery.

The taxonomy is frozen dataclasses — typed, hashable, trivially
JSON-serializable via ``asdict()``. The per-class ``kind`` string
is the SSE ``event: <name>`` field the wire ends up with. Callers
build events by instantiating the right subclass; the discriminator
field gets the correct default.

Pure Python; no LangGraph import at module top level. The bridge to
``get_stream_writer()`` is lazy + defensive so this module imports
cleanly in the main ``claude-hooks`` test env without LangGraph
installed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

log = logging.getLogger("consultants.engine.events")


# ============================================================== #
# Event taxonomy
# ============================================================== #

@dataclass(frozen=True, kw_only=True)
class CouncilEvent:
    """Base for every typed council event.

    Subclasses override ``kind`` with a string discriminator that's
    used as the SSE ``event:`` field name AND as the recorder row
    type. The ``ts`` field defaults to ``time.time()`` at construction
    so callers don't repeat themselves.

    The optional ``sid`` field lets the SSE bridge tag every event
    with its consultation ID even though LangGraph's
    ``astream_events`` already carries thread metadata — callers who
    care about cross-thread aggregation can pin it explicitly.
    """

    kind: str
    ts: float = field(default_factory=time.time)
    sid: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shallow dict.

        ``asdict()`` recursively converts nested dataclasses + dicts
        + lists, so payloads that embed e.g. tool args survive
        ``json.dumps(..., default=str)`` cleanly.
        """
        return asdict(self)


@dataclass(frozen=True, kw_only=True)
class NodeStarted(CouncilEvent):
    """A node has begun execution. Emitted at entry.

    Carried fields are the minimum a consumer needs to render
    "researcher (lane 2, glm-5.1:cloud) running round 2" without
    cross-referencing the recorder.
    """
    role: str
    round: int = 1
    lane_idx: Optional[int] = None
    model: Optional[str] = None
    kind: str = "node_started"


@dataclass(frozen=True, kw_only=True)
class NodeFinished(CouncilEvent):
    """Counterpart to NodeStarted. ``duration_ms`` is the wall time
    the node spent, ``ok`` distinguishes happy-path from tombstoned
    lanes / role failures.
    """
    role: str
    round: int = 1
    lane_idx: Optional[int] = None
    duration_ms: int = 0
    ok: bool = True
    error: Optional[str] = None
    kind: str = "node_finished"


@dataclass(frozen=True, kw_only=True)
class ToolCall(CouncilEvent):
    """One tool invocation. The researcher and (M6) tool_executor
    emit these; consumers render them as a sub-timeline under the
    parent node.

    ``args_preview`` and ``output_preview`` are short (≤ 200 chars)
    truncations of the full args/output — the bulk material lands
    in the recorder's existing ``events`` table. The streaming
    consumer doesn't need the full body to render a live feed.
    """
    role: str
    round: int = 1
    lane_idx: Optional[int] = None
    tool: str = ""
    args_preview: str = ""
    output_preview: str = ""
    duration_ms: int = 0
    error: Optional[str] = None
    kind: str = "tool_call"


@dataclass(frozen=True, kw_only=True)
class PartialSynthesis(CouncilEvent):
    """The synthesizer has emitted a draft delta. The streaming
    consumer can render this as the live answer-in-progress.

    ``text`` is the new content delta only; consumers concatenate
    successive deltas into the running buffer. ``draft_index`` lets
    the consumer notice gaps if events are dropped or reordered.
    """
    text: str
    draft_index: int = 0
    kind: str = "partial_synthesis"


@dataclass(frozen=True, kw_only=True)
class ConfidenceUpdate(CouncilEvent):
    """Synthesizer's self-rating or critic's verdict-confidence
    update. Drives the xauto escalator at M7.

    ``score`` is 0.0-1.0; ``source`` identifies whether it came from
    the synthesizer (self-rating block) or the critic (verdict
    confidence). ``target`` is the active threshold from
    ``runtime_control.confidence_target`` so the consumer can show
    "0.62 / 0.70 — below threshold".
    """
    score: float
    source: str = "synthesizer"
    target: Optional[float] = None
    kind: str = "confidence_update"


@dataclass(frozen=True, kw_only=True)
class DeadlineWarning(CouncilEvent):
    """Emitted by a node when ``runtime_control.deadline_ts`` is
    close OR has been passed. The plan §M5 review-before-synthesis
    interrupt may use this signal to decide whether to pause for
    user input.
    """
    remaining_s: float = 0.0
    soft_target_passed: bool = False
    hard_cap_passed: bool = False
    kind: str = "deadline_warning"


@dataclass(frozen=True, kw_only=True)
class RuntimeMutation(CouncilEvent):
    """Emitted when ``runtime_control`` is updated mid-flight (via
    POST /v1/consult/<sid>/control or graph.update_state from a
    test harness).

    ``changes`` is the dict of mutated keys (just the keys that
    changed; not the full RuntimeControl snapshot — that would
    bloat the stream). ``reason`` is a free-form string the caller
    supplies ("user cancel", "xauto escalation: xmedium -> xhigh").
    """
    changes: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    kind: str = "runtime_mutation"


@dataclass(frozen=True, kw_only=True)
class Interrupt(CouncilEvent):
    """Emitted right before a LangGraph ``interrupt()`` call so the
    consumer knows the council is parked waiting for input.

    ``payload_preview`` is a short summary of what the consumer is
    being asked to decide. The full snapshot lives in the
    checkpointer; ``thread_id`` lets a downstream tool fetch it.
    """
    interrupt_kind: str
    payload_preview: str = ""
    thread_id: Optional[str] = None
    kind: str = "interrupt"


@dataclass(frozen=True, kw_only=True)
class Resumed(CouncilEvent):
    """Emitted on the first node entry after a ``Command(resume=...)``
    unblocks an interrupt. ``decision`` captures what the resumer
    sent (typically the user's "approve" / "edit" / "abort" choice).
    """
    interrupt_kind: str
    decision: str = ""
    kind: str = "resumed"


# ============================================================== #
# Stream-writer bridge
# ============================================================== #

def emit(event: CouncilEvent) -> bool:
    """Push ``event`` onto LangGraph's custom-event stream.

    Uses ``langchain_core.callbacks.manager.dispatch_custom_event``
    (sync) which surfaces on ``compiled.astream_events(version="v2")``
    as records of shape::

        {"event": "on_custom_event",
         "name":  event.kind,
         "data":  event.to_dict(),
         "metadata": {...},
         "run_id": ...}

    The M4 SSE bridge demuxes those records into
    ``event: <kind>`` SSE messages on the wire.

    Defensive: when there's no runnable context (test code that
    isn't running through a compiled graph), this silently returns
    ``False`` instead of raising. That makes ``emit()`` safe to
    sprinkle inside node functions that ALSO get unit-tested as
    plain Python.

    Returns ``True`` when delivery succeeded, ``False`` otherwise.
    The recorder layer still writes the event to disk via
    ``MessageRecorder.record_event`` regardless of streaming
    delivery — that path is the post-mortem channel and doesn't
    depend on a live consumer.
    """
    try:
        from langchain_core.callbacks.manager import (
            dispatch_custom_event,
        )
    except ImportError:
        return False
    try:
        dispatch_custom_event(event.kind, event.to_dict())
        return True
    except RuntimeError:
        # Called outside of a runnable context (node ran as plain
        # function in a test). Drop silently.
        return False
    except Exception:  # pragma: no cover
        log.exception("emit: dispatch_custom_event raised; event dropped")
        return False


__all__ = [
    "CouncilEvent",
    "NodeStarted",
    "NodeFinished",
    "ToolCall",
    "PartialSynthesis",
    "ConfidenceUpdate",
    "DeadlineWarning",
    "RuntimeMutation",
    "Interrupt",
    "Resumed",
    "emit",
]
