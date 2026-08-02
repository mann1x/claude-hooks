"""The approval channel for the council's tool permission ladder.

M-A's remaining piece (`docs/PLAN-council-tool-surface.md`). The ladder
— ``auto`` / ``ask_assistant`` / ``ask_human`` / ``deny`` — has been
enforced at dispatch since the registry landed, but nothing was wired
to the ``ask_*`` rungs, so ``ToolRegistry`` refused them for want of an
approval channel. This module is that channel.

Two rungs, two very different behaviours, per the plan's table:

``ask_assistant``
    "auto-approve with discretion, **not** wait for a verdict." It
    approves immediately and records the decision. Making this stall
    would defeat the reason ``auto`` exists — routing a write through a
    round-trip on every lane burns tokens and wall-clock for a verdict
    that is "yes" by construction. What it buys over ``auto`` is the
    audit trail and the event, so the assistant *sees* the call and can
    tighten the rung afterwards.

``ask_human``
    The only rung that can stall, which is why the plan reserves it for
    spend. The lane parks, an ``awaiting_tool_approval`` event carries
    the tool, its arguments, the root it would run in and the rung it
    tripped, and the runner blocks until someone answers or the
    deadline passes.

**Timeout denies.** Decision-table row 7: *absence of a human never
authorizes spend.* The lane gets ``error: approval timed out``, the
model reroutes, and the council finishes degraded with provenance
rather than silently spending money nobody approved.

**Per-lane parking comes free.** The plan calls lane-scoped suspension
the largest piece of M-A because a graph-level interrupt would idle
every sibling on exactly the x-tier runs that matter most. Blocking
inside the tool executor achieves it without touching the graph:
LangGraph runs sync nodes on its own worker threads, so a lane waiting
here holds nothing but its own thread while its N×M siblings keep
going. (`feedback_xtier_diversity_priority`.)

Deliberately stdlib-only — no LangGraph, no FastAPI — so the main
claude-hooks test suite can drive the whole channel.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("consultants.engine.tool_approval")

#: How long a parked ``ask_human`` call waits before it is denied.
#: Ten minutes matches the adversary checkpoint's default: long enough
#: that a person who stepped away can still answer, short enough that
#: an unattended run doesn't hold a lane for the session's lifetime.
DEFAULT_APPROVAL_TIMEOUT_S = 600.0

#: Poll cadence of the wait loop. The wait is a blocked worker thread,
#: so this only bounds how fast an answer is noticed.
_POLL_INTERVAL_S = 0.5

_ids = itertools.count(1)


@dataclass
class ApprovalRequest:
    """One parked tool call awaiting a verdict.

    The payload is what an approver needs to judge it — the plan is
    explicit that "an approver cannot judge ``sh -c "..."`` on its
    own": the tool, its arguments, the root it would run in, and which
    rung it tripped.
    """
    request_id: str
    sid: str
    tool: str
    level: str
    arguments: str
    cwd: str
    reason: str
    opened_at: float
    deadline_ts: float
    lane_idx: Optional[int] = None
    role: Optional[str] = None
    #: None while pending; True/False once answered or timed out.
    allowed: Optional[bool] = None
    resolution: Optional[str] = None   # "allowed" | "denied" | "timeout"
    resolved_by: Optional[str] = None
    resolved_at: Optional[float] = None

    def public_dict(self) -> dict:
        """The shape GET /state and the SSE event both carry."""
        out = {
            "request_id": self.request_id,
            "tool": self.tool,
            "level": self.level,
            "arguments": self.arguments,
            "cwd": self.cwd,
            "reason": self.reason,
            "opened_at": self.opened_at,
            "deadline_ts": self.deadline_ts,
        }
        if self.lane_idx is not None:
            out["lane_idx"] = self.lane_idx
        if self.role is not None:
            out["role"] = self.role
        if self.resolution is not None:
            out["resolution"] = self.resolution
            out["allowed"] = self.allowed
            out["resolved_by"] = self.resolved_by
        return out


class ToolApprovalBroker:
    """Thread-safe registry of parked approval requests for one session.

    Lives on ``SessionState``. The runner's worker threads open and wait;
    the HTTP handler resolves from a different thread — the same
    cross-thread shape as the adversary ack, and guarded the same way.
    """

    def __init__(self, sid: str = ""):
        self.sid = sid
        self._lock = threading.RLock()
        self._pending: dict[str, ApprovalRequest] = {}
        self._history: list[ApprovalRequest] = []

    # ---------------------------------------------------------------- #
    def open(self, *, tool: str, level: str, arguments: str, cwd: str,
             reason: str, timeout_s: float,
             lane_idx: Optional[int] = None,
             role: Optional[str] = None,
             now: Optional[float] = None) -> ApprovalRequest:
        now = time.time() if now is None else now
        req = ApprovalRequest(
            request_id=f"tap-{next(_ids)}",
            sid=self.sid, tool=tool, level=level, arguments=arguments,
            cwd=cwd, reason=reason, opened_at=now,
            deadline_ts=now + max(0.0, float(timeout_s)),
            lane_idx=lane_idx, role=role,
        )
        with self._lock:
            self._pending[req.request_id] = req
        return req

    def resolve(self, request_id: Optional[str], *, allow: bool,
                by: str = "assistant",
                now: Optional[float] = None) -> Optional[ApprovalRequest]:
        """Answer one request. ``request_id=None`` answers the oldest
        pending one — the common case is a single parked call, and
        making the operator quote an id for it is friction with no
        safety value. Returns the request, or ``None`` when there was
        nothing pending (a duplicate ack is a harmless no-op)."""
        now = time.time() if now is None else now
        with self._lock:
            if request_id is None:
                if not self._pending:
                    return None
                request_id = min(
                    self._pending,
                    key=lambda k: self._pending[k].opened_at,
                )
            req = self._pending.pop(request_id, None)
            if req is None:
                return None
            req.allowed = bool(allow)
            req.resolution = "allowed" if allow else "denied"
            req.resolved_by = by
            req.resolved_at = now
            self._history.append(req)
            return req

    def expire(self, request_id: str,
               now: Optional[float] = None) -> Optional[ApprovalRequest]:
        """Mark a request denied because its deadline passed."""
        now = time.time() if now is None else now
        with self._lock:
            req = self._pending.pop(request_id, None)
            if req is None:
                return None
            req.allowed = False
            req.resolution = "timeout"
            req.resolved_by = "timeout"
            req.resolved_at = now
            self._history.append(req)
            return req

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            req = self._pending.get(request_id)
            if req is not None:
                return req
            for past in reversed(self._history):
                if past.request_id == request_id:
                    return past
        return None

    def pending(self) -> list[ApprovalRequest]:
        with self._lock:
            return sorted(self._pending.values(),
                          key=lambda r: r.opened_at)

    def pending_public(self) -> list[dict]:
        return [r.public_dict() for r in self.pending()]

    def history_public(self) -> list[dict]:
        with self._lock:
            return [r.public_dict() for r in self._history]


# -------------------------------------------------------------------- #
# The approval function the registry calls.
# -------------------------------------------------------------------- #

@dataclass
class ApprovalContext:
    """Everything ``make_approval_fn`` needs that isn't the decision."""
    broker: ToolApprovalBroker
    cwd: str = ""
    timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S
    #: Called with (kind, payload) for every state change. The runner
    #: passes a recorder/SSE emitter; tests pass a list append.
    emit: Optional[Callable[[str, dict], None]] = None
    #: Returns True when the session is gone (cancelled / reaped), so a
    #: parked lane doesn't hold a thread on a dead run.
    is_closed: Optional[Callable[[], bool]] = None
    now_fn: Callable[[], float] = time.time
    sleep_fn: Callable[[float], None] = time.sleep
    #: Recorded so a caller can assert the channel was actually used.
    stats: dict = field(default_factory=lambda: {
        "auto_approved": 0, "granted": 0, "denied": 0, "timed_out": 0,
    })


def _emit(ctx: ApprovalContext, kind: str, payload: dict) -> None:
    if ctx.emit is None:
        return
    try:
        ctx.emit(kind, payload)
    except Exception:  # pragma: no cover — telemetry must not break a run
        log.exception("approval emit(%s) raised", kind)


def make_approval_fn(ctx: ApprovalContext) -> Callable[[Any], bool]:
    """Return the ``approval_fn`` ``ToolRegistry`` calls on an ``ask_*``.

    The registry's contract is a plain ``(decision) -> bool``; anything
    raised is caught upstream and treated as a refusal, and a refusal
    surfaces to the model as a tool-result string rather than an
    exception (the plan is explicit that a denied tool must teach the
    model to try another route, not crash the lane).
    """
    def _approve(decision: Any) -> bool:
        level = str(getattr(decision, "level", "") or "")
        tool = str(getattr(decision, "tool", "") or "?")
        reason = str(getattr(decision, "reason", "") or "")
        # ``GateDecision.raw_args`` is the field name; keep the
        # ``arguments`` fallback so a future decision type with the
        # friendlier name still shows the approver what would run.
        arguments = str(
            getattr(decision, "raw_args", "")
            or getattr(decision, "arguments", "")
            or ""
        )

        # ask_assistant: approve now, record it, never stall.
        if level == "ask_assistant":
            ctx.stats["auto_approved"] += 1
            _emit(ctx, "tool_approval_auto", {
                "tool": tool, "level": level, "cwd": ctx.cwd,
                "arguments": arguments, "reason": reason,
            })
            log.info("tool %s auto-approved (%s)", tool, level)
            return True

        # ask_human: park the lane.
        req = ctx.broker.open(
            tool=tool, level=level, arguments=arguments, cwd=ctx.cwd,
            reason=reason, timeout_s=ctx.timeout_s,
            lane_idx=getattr(decision, "lane_idx", None),
            role=getattr(decision, "role", None),
            now=ctx.now_fn(),
        )
        _emit(ctx, "awaiting_tool_approval", req.public_dict())
        log.warning(
            "tool %s needs %s approval — parked (request_id=%s "
            "deadline=%.0f timeout=%ss)",
            tool, level, req.request_id, req.deadline_ts, ctx.timeout_s,
        )

        while ctx.now_fn() < req.deadline_ts:
            current = ctx.broker.get(req.request_id)
            if current is not None and current.resolution is not None:
                break
            if ctx.is_closed is not None and ctx.is_closed():
                # Session cancelled or reaped out from under the park.
                break
            remaining = req.deadline_ts - ctx.now_fn()
            if remaining <= 0:
                break
            ctx.sleep_fn(min(_POLL_INTERVAL_S, remaining))

        resolved = ctx.broker.get(req.request_id)
        if resolved is None or resolved.resolution is None:
            resolved = ctx.broker.expire(req.request_id, now=ctx.now_fn())
        if resolved is None:  # pragma: no cover — defensive
            ctx.stats["denied"] += 1
            return False

        if resolved.resolution == "timeout":
            ctx.stats["timed_out"] += 1
            log.warning(
                "tool %s approval timed out after %ss — DENIED "
                "(absence of an approver never authorizes spend)",
                tool, ctx.timeout_s,
            )
        elif resolved.allowed:
            ctx.stats["granted"] += 1
        else:
            ctx.stats["denied"] += 1
        _emit(ctx, "tool_approval_resolved", resolved.public_dict())
        return bool(resolved.allowed)

    return _approve


__all__ = [
    "ApprovalContext",
    "ApprovalRequest",
    "DEFAULT_APPROVAL_TIMEOUT_S",
    "ToolApprovalBroker",
    "make_approval_fn",
]
