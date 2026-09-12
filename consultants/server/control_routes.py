"""M9 HTTP control surface — FastAPI route handlers for the live
LangGraph control endpoints.

The pure-Python payload builders live in
:mod:`consultants.server.control` (M5). This module is the thin
adapter between FastAPI request bodies and those builders, plus
the LangGraph-side application: ``graph.update_state`` for
inject/control/interrupt/cancel, ``graph.invoke(Command(resume=…))``
for resume, ``graph.get_state`` for state, and the SSE bridge
(:mod:`consultants.server.events_sse`) for /events.

Endpoint summary (all under ``/v1/consult/{sid}/``)::

    GET  /state      -> live or last-known StateSnapshot (summary)
    POST /inject     -> apply additional_context delta
    POST /control    -> mutate runtime_control (deadline, caps, …)
    POST /interrupt  -> flip pause_requested on runtime_control
    POST /resume     -> Command(resume=...) re-entry (async)
    POST /cancel     -> flip cancel_requested + optionally drop checkpoint
    GET  /events     -> SSE replay + live stream over runtime_events

The handlers share a small helper layer
(:func:`_require_session`, :func:`_require_live_session`,
:func:`_safe_apply_state_delta`) so the lifecycle rules are
expressed once and uniformly. Lifecycle:

- ``404`` — session not found (in memory or on disk).
- ``410`` — session has been closed (idle reap, explicit close).
- ``409`` — session is in a state that doesn't accept this verb
  (e.g. POST /inject after the run completed).
- ``503`` — runner not configured, or the live graph handles
  aren't attached yet (race between create + run).
- ``400`` — payload validation failure surfaced from the builder.

Tests live in ``tests/test_consultants_v2_control_routes.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Optional


if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from fastapi import FastAPI


# FastAPI is a hard dep of this module. The route handlers reference
# ``Request`` in their signatures; FastAPI introspects the
# annotation at registration time, so the import must resolve at
# module load — a lazy import inside ``register_control_routes``
# leaves the annotation as a bare string and FastAPI treats
# ``request`` as a missing query param (422). The consultants env
# always has fastapi installed; the main env imports
# ``control.py`` (builders only) which has no fastapi dep.
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse


from consultants.server.control import (
    ControlInputError,
    INJECT_STATUS_APPLIED,
    INJECT_STATUS_FAILED,
    INJECT_STATUS_PENDING,
    INJECT_STATUS_REJECTED,
    Injection,
    PHASE_SYNTHESIS,
    PHASE_TERMINAL,
    ROUTED_BEST_EFFORT_CAP_REACHED,
    ROUTED_IN_PLACE,
    ROUTED_QUEUED,
    ROUTED_REWOUND_TO_RESEARCHER,
    build_cancel_request,
    build_inject_delta,
    build_interrupt_delta,
    build_resume_command,
    build_runtime_control_delta,
    classify_phase,
    default_target_for_phase,
    rewind_budget_ok,
    summarize_state_for_get,
)


log = logging.getLogger("consultants.server.control_routes")


# Status codes hoisted as module constants so handlers stay
# declarative — and tests can assert on them without re-typing
# literals scattered across the file.
_HTTP_BAD_REQUEST = 400
_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409
_HTTP_GONE = 410
_HTTP_INTERNAL = 500
_HTTP_UNAVAILABLE = 503


# ============================================================== #
# Lifecycle helpers
# ============================================================== #

def _require_session(app, sid: str):
    """Look up a SessionState in memory. ``404`` if unknown.

    Disk-fallback (the v1 path that re-loads a finished session
    from artifacts) is intentionally NOT hooked here — the control
    verbs only make sense on live in-memory sessions. ``GET /state``
    on a closed session that's gone from memory should use the
    existing ``GET /v1/consult/{sid}`` poll endpoint, which already
    has the disk fallback.
    """
    sessions = getattr(app.state, "sessions", None) or {}
    s = sessions.get(sid)
    if s is None:
        raise HTTPException(
            status_code=_HTTP_NOT_FOUND,
            detail=f"session not found: {sid}",
        )
    return s


def _require_live_session(app, sid: str, *, allow_paused: bool = True):
    """Look up a session and assert it can accept a control verb.

    - 404 when unknown.
    - 410 when closed (idle reap / explicit close).
    - 409 when status is ``completed`` / ``failed`` (run is over —
      mutations are a no-op semantically, so we reject them
      explicitly rather than silently swallow).
    - 503 when the runner hasn't attached the live graph handles
      yet. This is a millisecond-window race between
      ``executor.submit`` and the runner's first line; clients
      should retry.

    ``allow_paused`` is True by default — pause-and-then-mutate is
    the standard HITL flow. Set False from /resume (you don't
    re-resume something that isn't paused).
    """
    s = _require_session(app, sid)
    if getattr(s, "closed", False):
        raise HTTPException(
            status_code=_HTTP_GONE,
            detail=f"session closed: {sid}",
        )
    status = getattr(s, "status", "")
    if status not in ("running",):
        raise HTTPException(
            status_code=_HTTP_CONFLICT,
            detail=(
                f"session is {status!r}; control verbs require a "
                f"running session"
            ),
        )
    if (getattr(s, "_compiled", None) is None
            or getattr(s, "_thread_config", None) is None):
        raise HTTPException(
            status_code=_HTTP_UNAVAILABLE,
            detail=(
                f"session {sid} has no live graph handles yet — "
                "the runner is still spinning up; retry"
            ),
        )
    return s


def _apply_state_delta_core(
    s, delta: dict, *,
    as_node: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Apply ``compiled.update_state`` and return ``(ok, error)``.

    Never raises — the error string is returned so callers can decide
    how to surface it (a 500 for the legacy verbs, an ``failed``
    inject status for the M9 inject contract). This is the shared
    core the inject drain ``apply_fn`` builds on.
    """
    try:
        if as_node is not None:
            s._compiled.update_state(s._thread_config, delta,
                                       as_node=as_node)
        else:
            s._compiled.update_state(s._thread_config, delta)
        return True, None
    except Exception as e:  # pragma: no cover — defensive
        log.exception("update_state raised for sid=%s", s.sid)
        return False, f"{type(e).__name__}: {e}"


def _safe_apply_state_delta(
    s, delta: dict, *,
    as_node: Optional[str] = None,
) -> None:
    """Wrap :func:`_apply_state_delta_core` so a LangGraph error
    becomes a 500 with a structured detail (not a bare stack trace).

    Re-raises as ``HTTPException(500, …)`` — keeps the HTTP contract
    sane for the control / interrupt / resume / cancel verbs. The
    inject verb uses the non-raising core directly.
    """
    ok, err = _apply_state_delta_core(s, delta, as_node=as_node)
    if not ok:
        raise HTTPException(
            status_code=_HTTP_INTERNAL,
            detail=f"update_state failed: {err}",
        )


def _new_injection_id() -> str:
    """Short, sortable inject id like ``inj-1716563820-3f9a``."""
    import secrets
    return f"inj-{int(time.time())}-{secrets.token_hex(2)}"


def _record_inject_event(s, inj) -> None:
    """Best-effort: persist the inject lifecycle to the recorder's
    ``runtime_events`` table as ``kind="inject"`` so it (a) survives a
    restart for audit, (b) replays over SSE via Last-Event-ID, and
    (c) shows up live on the /events stream. Never raises — the
    inject's effect on graph state is the source of truth; the
    runtime_event row is observability."""
    recorder = getattr(s, "_recorder", None)
    if recorder is None or not hasattr(recorder, "record_event"):
        return
    try:
        recorder.record_event(
            kind="inject",
            role=inj.target_role or inj.role,
            payload={"sid": s.sid, **inj.to_public()},
        )
    except Exception:  # pragma: no cover — defensive
        log.exception("record_event(kind=inject) failed for sid=%s", s.sid)


def _classify_phase_from_live(s) -> tuple[str, dict]:
    """Read the live LangGraph snapshot and return ``(phase, values)``.

    Raises on a get_state failure so the caller marks the inject
    ``failed`` (the graph was supposed to be ready). ``values`` is the
    snapshot's state dict (unused by the safe layer; the rewind
    follow-up reads its reroute counters from it).
    """
    snapshot = s._compiled.get_state(s._thread_config)
    next_nodes = list(getattr(snapshot, "next", ()) or ())
    values = getattr(snapshot, "values", {}) or {}
    phase = classify_phase(
        next_nodes=next_nodes,
        status=getattr(s, "status", ""),
        closed=bool(getattr(s, "closed", False)),
    )
    return phase, values


def _make_inject_apply_fn(s):
    """Build the ``apply_fn(inj)`` the synchronous inject path AND the
    drain share. Mutates ``inj`` in place to a terminal status,
    rebuilding the inject Doc delta from the injection record itself so
    one closure serves both call sites.

    Safe-layer routing (rewind realization is a gated follow-up):
    classify the council phase from the live snapshot, compute the
    phase-aware ``target_role`` (the role this inject is *for*), write
    the inject Doc into ``additional_context`` exactly as
    :func:`build_inject_delta` produces it (preserving the M5
    broad-surfacing semantics for ``role="any"``), and record
    ``routed=in_place``. A synthesis-phase inject is applied in place
    to the synthesizer and honestly reports ``in_place`` — it is NOT
    rewound until the follow-up ships.
    """
    def apply_fn(inj) -> None:
        # Re-check terminal at apply time (a drain may run after the
        # council finished).
        if (getattr(s, "closed", False)
                or getattr(s, "status", "") in ("completed", "failed")):
            inj.status = INJECT_STATUS_REJECTED
            inj.phase_at_apply = PHASE_TERMINAL
            inj.error = f"session is {getattr(s, 'status', '')!r}"
            return
        if (getattr(s, "_compiled", None) is None
                or getattr(s, "_thread_config", None) is None):
            # Still no live graph — leave pending for the next drain.
            inj.status = INJECT_STATUS_PENDING
            inj.routed = ROUTED_QUEUED
            return
        try:
            delta = build_inject_delta(
                role=inj.role, text=inj.text,
                source=inj.source, ts=inj.ts,
            )
        except ControlInputError as e:
            inj.status = INJECT_STATUS_FAILED
            inj.error = str(e)
            return
        try:
            phase, values = _classify_phase_from_live(s)
        except Exception as e:
            inj.status = INJECT_STATUS_FAILED
            inj.error = f"get_state: {type(e).__name__}: {e}"
            return
        if phase == PHASE_TERMINAL:
            inj.status = INJECT_STATUS_REJECTED
            inj.phase_at_apply = PHASE_TERMINAL
            inj.error = "council reached terminal phase before apply"
            return
        effective_target = (
            inj.role if inj.role and inj.role != "any"
            else default_target_for_phase(phase)
        )
        inj.phase_at_apply = phase
        inj.target_role = effective_target

        if phase == PHASE_SYNTHESIS:
            # #314: the council is parked at the always-on synthesizer
            # interrupt. Write the Doc with NO as_node so the parked
            # ``next`` (=synthesizer) is preserved — the runner, not
            # this write, drives the rewind/auto-resume. If the inject
            # targets the researcher (the phase default, or explicit)
            # AND the round budget allows, request a rewind: the runner
            # re-runs a researcher pass that picks up this Doc before
            # re-synthesizing. Otherwise the Doc still reaches the
            # synthesizer as text (in place / best-effort).
            ok, err = _apply_state_delta_core(s, delta)
            if not ok:
                inj.status = INJECT_STATUS_FAILED
                inj.error = err
                return
            if effective_target == "researcher":
                if rewind_budget_ok(values):
                    s.request_revalidation()
                    inj.routed = ROUTED_REWOUND_TO_RESEARCHER
                else:
                    inj.routed = ROUTED_BEST_EFFORT_CAP_REACHED
            else:
                # Explicit non-researcher target (e.g. synthesizer): the
                # synthesizer reads it in place on resume, no rewind.
                inj.routed = ROUTED_IN_PLACE
            inj.status = INJECT_STATUS_APPLIED
            inj.applied_at = time.time()
            return

        # Non-synthesis phases: the graph is mid-flight (or about to
        # run the target role). ``as_node="researcher"`` keeps
        # update_state happy (the node name must be registered); the
        # Doc's own ``role`` drives which node surfaces it.
        ok, err = _apply_state_delta_core(s, delta, as_node="researcher")
        if not ok:
            inj.status = INJECT_STATUS_FAILED
            inj.error = err
            return
        inj.routed = ROUTED_IN_PLACE
        inj.status = INJECT_STATUS_APPLIED
        inj.applied_at = time.time()

    return apply_fn


def _inject_response(s, inj, *, queue_position: Optional[int] = None) -> dict:
    """Uniform POST /inject body. Always ``ok=True`` (the HTTP status
    is always 200 — the lifecycle lives in ``status``)."""
    body: dict = {
        "ok": True,
        "status": inj.status,
        "injection_id": inj.id,
        "sid": s.sid,
        "role": inj.role,
        "target_role": inj.target_role,
        "phase_at_apply": inj.phase_at_apply,
        "routed": inj.routed,
    }
    if queue_position is not None:
        body["queue_position"] = queue_position
    if inj.error:
        body["error"] = inj.error
        if inj.status == INJECT_STATUS_REJECTED:
            body["reason"] = inj.error
    return body


def _snapshot_to_dict(snapshot: Any) -> dict:
    """Convert a LangGraph ``StateSnapshot`` (NamedTuple) to a
    dict shape :func:`summarize_state_for_get` understands.

    LangGraph snapshots have ``.values`` (state dict), ``.next``
    (tuple of pending nodes), ``.tasks`` (tuple of PregelTask).
    The summarizer reads them via dict access, so we mirror.
    """
    if snapshot is None:
        return {}
    if isinstance(snapshot, dict):
        return snapshot
    return {
        "values": getattr(snapshot, "values", {}) or {},
        "next": list(getattr(snapshot, "next", ()) or ()),
        "tasks": list(getattr(snapshot, "tasks", ()) or ()),
    }


# ============================================================== #
# Route registration
# ============================================================== #

def register_control_routes(app: "FastAPI") -> None:
    """Attach the seven M9 control endpoints to ``app``.

    Idempotent in spirit but FastAPI itself raises if you register
    the same path twice — ``create_app`` calls this exactly once.
    """
    # -------------------- GET /state --------------------------- #
    @app.get("/v1/consult/{sid}/state")
    def get_state(sid: str) -> dict:
        s = _require_session(app, sid)
        # A completed run can still be inspected; we return the
        # final state as a static snapshot. The live-graph path is
        # only used while the run is still in flight.
        if (getattr(s, "_compiled", None) is None
                or getattr(s, "_thread_config", None) is None):
            # No live graph (run finished + cleaned up, or never
            # had one). Synthesize a summary from SessionState
            # fields so callers always get the same shape.
            static = {
                "values": {
                    "research": list(getattr(s, "research", []) or []),
                    "plan_items": list(getattr(s, "plan_items", []) or []),
                    "research_rounds_used": 0,
                    "critic_reroutes_used": 0,
                    "critic_decision": None,
                    "partial_synthesis": None,
                    "additional_context": [],
                    "error": getattr(s, "error", None),
                    "final_answer": getattr(s, "final_answer", "") or "",
                },
                "next": [],
                "tasks": [],
            }
            payload = summarize_state_for_get(static, sid=sid)
            payload["status"] = getattr(s, "status", "")
            payload["closed"] = bool(getattr(s, "closed", False))
            payload["injections"] = s.injection_records()
            # Consultancy review-loop status (engine-owned, above the
            # per-run status). Lazy import avoids the app<->routes cycle.
            from consultants.server.app import _attach_consultancy
            _attach_consultancy(app, sid, payload)
            return payload
        # Opportunistic drain: a /inject that arrived during the
        # spin-up race queued as pending; now that the graph is live,
        # apply anything still waiting before snapshotting.
        if getattr(s, "_pending_injections", None):
            drained = s.drain_injections(_make_inject_apply_fn(s))
            for inj in drained:
                _record_inject_event(s, inj)
        try:
            snapshot = s._compiled.get_state(s._thread_config)
        except Exception as e:
            log.exception("get_state raised for sid=%s", sid)
            raise HTTPException(
                status_code=_HTTP_INTERNAL,
                detail=f"get_state failed: {type(e).__name__}: {e}",
            )
        payload = summarize_state_for_get(
            _snapshot_to_dict(snapshot), sid=sid,
        )
        payload["status"] = getattr(s, "status", "")
        payload["closed"] = bool(getattr(s, "closed", False))
        payload["injections"] = s.injection_records()
        from consultants.server.app import _attach_consultancy
        _attach_consultancy(app, sid, payload)
        s.bump_activity()
        return payload

    # -------------------- POST /inject ------------------------- #
    @app.post("/v1/consult/{sid}/inject")
    def inject(sid: str, body: dict) -> dict:
        # Uniform contract (2026-05-24): ALWAYS HTTP 200 + a ``status``
        # in the body (applied|pending|rejected|failed). The one
        # exception is payload validation → 400, because a malformed
        # request is the caller's bug, not a council state. The
        # assistant reads ``status`` to know whether to keep watching.
        s = _require_session(app, sid)  # 404 if unknown
        try:
            delta = build_inject_delta(
                role=str(body.get("role") or "any"),
                text=str(body.get("text") or ""),
                source=str(body.get("source") or "user"),
                ts=body.get("ts"),
            )
        except ControlInputError as e:
            raise HTTPException(_HTTP_BAD_REQUEST, str(e))
        doc = delta["additional_context"][0]
        requested_role = str(body.get("role") or "any")
        inj = Injection(
            id=_new_injection_id(),
            role=requested_role,
            text=doc.text,
            source=getattr(doc, "source", "user"),
            ts=doc.ts,
            content_hash=doc.content_hash,
        )

        # Idempotency: a retry of the same (role, text) returns the
        # original record's status instead of creating a duplicate —
        # mirrors the Doc reducer's content_hash dedup.
        if inj.content_hash:
            with s._inject_lock:
                for existing in s._injections:
                    if existing.content_hash == inj.content_hash:
                        return _inject_response(s, existing)

        # Terminal session → rejected (the council can't act on it).
        if (getattr(s, "closed", False)
                or getattr(s, "status", "") in ("completed", "failed")):
            inj.status = INJECT_STATUS_REJECTED
            inj.phase_at_apply = PHASE_TERMINAL
            inj.error = (
                "session closed" if getattr(s, "closed", False)
                else f"session is {getattr(s, 'status', '')!r}"
            )
            s.register_injection(inj)
            _record_inject_event(s, inj)
            return _inject_response(s, inj)

        apply_fn = _make_inject_apply_fn(s)

        # Spin-up race: the runner hasn't attached the live graph yet.
        # Queue as pending and drain when it does (runner + the next
        # /state|/inject poll both call drain_injections).
        if (getattr(s, "_compiled", None) is None
                or getattr(s, "_thread_config", None) is None):
            pos = s.enqueue_injection(inj)
            _record_inject_event(s, inj)
            return _inject_response(s, inj, queue_position=pos)

        # Live graph ready → apply synchronously.
        s.register_injection(inj)
        apply_fn(inj)
        if inj.status == INJECT_STATUS_PENDING:
            # Graph vanished between the check and the apply; hand it
            # to the pending queue for a later drain.
            with s._inject_lock:
                inj.routed = ROUTED_QUEUED
                s._pending_injections.append(inj)
        _record_inject_event(s, inj)
        s.bump_activity()
        return _inject_response(s, inj)

    # -------------------- POST /control ------------------------ #
    @app.post("/v1/consult/{sid}/control")
    def control(sid: str, body: dict) -> dict:
        s = _require_live_session(app, sid)
        rc_in = body.get("runtime_control")
        if not isinstance(rc_in, dict):
            raise HTTPException(
                _HTTP_BAD_REQUEST,
                "body.runtime_control must be a dict of "
                "partial-update fields",
            )
        # The builder takes a single dict of changes (NOT kwargs)
        # so it can validate per-key without splatting through the
        # function signature. Unknown keys raise ControlInputError
        # which maps to 400 below.
        try:
            delta = build_runtime_control_delta(rc_in)
        except ControlInputError as e:
            raise HTTPException(_HTTP_BAD_REQUEST, str(e))
        _safe_apply_state_delta(s, delta)
        s.bump_activity()
        return {"ok": True, "applied": _serialize_for_json(delta)}

    # -------------------- POST /interrupt ---------------------- #
    @app.post("/v1/consult/{sid}/interrupt")
    def interrupt_(sid: str, body: Optional[dict] = None) -> dict:
        """Pause the run at the next node boundary.

        The state delta below is kept as the *record* of the request.
        The delta alone never paused anything — a graph that is already
        inside ``invoke`` carries its channels in memory and never
        re-reads the checkpoint ``update_state`` writes — so the pause
        that actually takes effect is the out-of-band one on
        ``run_control``. The next node to enter parks inside its own
        worker thread, which means its x-tier siblings keep running;
        pausing the graph instead would idle the whole fanout.
        """
        s = _require_live_session(app, sid)
        body = body or {}
        reason = str(body.get("reason") or "user-pause")
        delta = build_interrupt_delta(reason=reason)
        _safe_apply_state_delta(s, delta)
        paused = s.run_control.request_pause(reason)
        s.bump_activity()
        snap = s.run_control.snapshot()
        out = {
            "ok": True,
            "applied": _serialize_for_json(delta),
            # False when the run is already cancelling — pausing a run
            # that is draining would park a node that should be
            # finishing.
            "paused": bool(paused),
            # "pending" until a node reaches the gate. Reported at
            # request time because this is the moment the caller is
            # looking, and "ok: true" on its own reads as "the run has
            # stopped" when it has not yet.
            "pause_state": snap.get("pause_state"),
            "pause_deadline_ts": snap.get("pause_deadline_ts"),
        }
        if paused:
            out.update(s._pause_blockers())
        return out

    # -------------------- POST /resume ------------------------- #
    @app.post("/v1/consult/{sid}/resume")
    def resume(sid: str, body: Optional[dict] = None) -> dict:
        # Resume re-enters the graph at the interrupt point with
        # Command(resume=value). We can't reuse the runner's stream
        # loop (it's already returned at the interrupt). We DO need
        # to keep the HTTP request short, so the actual re-invoke
        # gets queued on the executor and the handler returns 202.
        s = _require_live_session(app, sid)
        body = body or {}

        # M2: while the runner owns the graph for an adversary checkpoint
        # — the pause AND the rewind / auto-resume re-stream the ack
        # kicks off — the runner is the SOLE resumer. A /resume here must
        # NOT submit an executor invoke (that would double-resume against
        # the live, mid-stream runner). Delegate to the ack path: flip the
        # flag the runner's poll reads and return. ``_adversary_checkpoint_active``
        # spans the whole runner-owned window (set before the pause,
        # cleared when the drive loop returns), unlike ``_checkpoint_deadline_ts``
        # which is set only during the blocking wait — the broader flag is
        # what closes the post-ack re-stream double-resume window. Read
        # under the lock so the check races neither the runner's set nor
        # its clear.
        #
        # A node parked on ``POST /interrupt`` is checked FIRST, and
        # ``_adversary_checkpoint_active`` is why: that flag spans the
        # whole runner-owned window, so it can still be set long after
        # the checkpoint itself was acked. Testing it first swallowed
        # the pause release — observed live on
        # ``csl-2026-08-02-1042-1036``, where /resume returned
        # ``adversary_ack`` while the synthesizer stayed parked with no
        # way to free it. ``run_control.paused`` is the precise
        # condition: true only while a pause is actually outstanding.
        #
        # Both can be set at once, and then both get released — the ack
        # is idempotent, and returning after only one would leave the
        # run blocked on the other.
        released_pause = s.run_control.paused
        if released_pause:
            # The parked node is blocked inside its own worker thread,
            # so releasing the flag IS the entire resume; nothing
            # re-enters the graph.
            s.run_control.release_pause(by="resume")
        with s._inject_lock:
            checkpoint_active = bool(
                getattr(s, "_adversary_checkpoint_active", False))
        if checkpoint_active:
            s.ack_adversary()
        if released_pause or checkpoint_active:
            s.bump_activity()
            modes = []
            if released_pause:
                modes.append("pause_release")
            if checkpoint_active:
                modes.append("adversary_ack")
            return {
                "ok": True,
                "mode": "+".join(modes),
                "resumed": bool(released_pause),
                "acked": bool(checkpoint_active),
                "checkpoint_open": getattr(
                    s, "_checkpoint_deadline_ts", None) is not None,
            }

        value = body.get("value")
        decision = str(body.get("decision") or "")
        resume_cmd = build_resume_command(value, decision=decision)

        # Clear the interrupt_state channel BEFORE re-entry so the
        # next-node guard in interrupt_policy doesn't immediately
        # re-trigger the pause. update_state respects the reducer
        # for runtime_control (merge); pause_requested goes false.
        from consultants.engine.interrupt_policy import clear_interrupt
        clear_delta = clear_interrupt({})
        _safe_apply_state_delta(s, clear_delta)

        # Schedule the re-invoke on the executor. Use lazy import
        # to avoid the top-level dependency cycle (app imports
        # this module; the runner imports app).
        from concurrent.futures import Future
        try:
            from langgraph.types import Command
        except ImportError as e:
            raise HTTPException(
                _HTTP_UNAVAILABLE,
                f"langgraph not available: {e}",
            )

        def _resume_in_executor():
            try:
                # invoke (not stream) — we want to drive to the
                # next interrupt / END synchronously.
                s._compiled.invoke(
                    Command(resume=resume_cmd.value),
                    config=s._thread_config,
                )
            except Exception:  # pragma: no cover — runner-side
                log.exception("resume re-invoke failed for sid=%s", sid)

        executor = getattr(app.state, "executor", None)
        if executor is None:
            # No executor — run inline (test path).
            _resume_in_executor()
            return {"ok": True, "mode": "inline",
                    "resume_value": _serialize_for_json(value)}
        fut: Future = executor.submit(_resume_in_executor)
        s.bump_activity()
        return {
            "ok": True,
            "mode": "scheduled",
            "resume_value": _serialize_for_json(value),
            "future_id": id(fut),
        }

    # -------------------- POST /adversary-ack ------------------ #
    @app.post("/v1/consult/{sid}/adversary-ack")
    def adversary_ack(sid: str, body: Optional[dict] = None) -> dict:
        """M2: release the engine adversary checkpoint early.

        Unlike /resume, this does NOT re-invoke the graph — it only
        flips a flag the runner's checkpoint poll-loop reads. The runner
        is the sole resumer (it never returned at the checkpoint), so an
        executor re-invoke here would double-resume the graph. The body
        is ignored except for an optional ``reason`` recorded for
        post-mortem.

        Returns ``acked: true`` whenever the session is live, whether or
        not a checkpoint is currently open. The flag is read-and-cleared
        by the runner, so an ack that lands BEFORE the checkpoint opens
        (a consumer that pre-decides "proceed, no challenge") is honored:
        the first poll of the wait loop consumes it and releases
        immediately — the runner does NOT clear it on entry. A duplicate
        ack after release is a harmless no-op. ``checkpoint_open`` echoes
        whether a wait is currently blocking so the caller can tell
        whether the ack landed on an active pause."""
        s = _require_live_session(app, sid)
        s.ack_adversary()
        s.bump_activity()
        deadline = getattr(s, "_checkpoint_deadline_ts", None)
        return {
            "ok": True,
            "acked": True,
            "checkpoint_open": deadline is not None,
            "deadline_ts": deadline,
        }

    # -------------------- POST /tool-ack ----------------------- #
    @app.post("/v1/consult/{sid}/tool-ack")
    def tool_ack(sid: str, body: Optional[dict] = None) -> dict:
        """M-A: answer a parked ``ask_human`` tool-approval request.

        Body: ``{"allow": true|false, "request_id": "tap-N"?,
        "reason": "..."?, "scope": "once"|"tool"|"glob"?,
        "pattern": "src/**"?}``. ``allow`` is REQUIRED — there is no
        default verdict, because guessing either way is the failure this
        channel exists to prevent. ``request_id`` is optional; omitted,
        it answers the oldest pending request, which is the common case
        of exactly one parked call.

        ``scope`` answers for a *class* of calls rather than one:
        ``"tool"`` covers every future call to the same tool, ``"glob"``
        covers calls whose target matches ``pattern`` (default: the
        answered call's own target). The rule is session-scoped —
        authorization is per council, so every role and every x-tier
        lane inherits it — and installing one also releases the parked
        requests it already matches. Without this the rung floods: three
        researcher lanes reading the same file produced three requests
        in the same second on the first live run.

        Like /adversary-ack this does not touch the graph: the lane is
        blocked inside its tool executor, so resolving the request is
        the whole resume. Answering when nothing is pending returns
        ``resolved: false`` rather than erroring — a duplicate ack after
        a timeout is a harmless no-op, and the timeout has already
        denied.
        """
        body = body or {}
        if "allow" not in body:
            raise HTTPException(400, "tool-ack requires 'allow'")
        allow = bool(body.get("allow"))
        request_id = body.get("request_id")
        if request_id is not None and not isinstance(request_id, str):
            raise HTTPException(400, "request_id must be a string")
        scope = str(body.get("scope") or "once")
        if scope not in ("once", "tool", "glob"):
            raise HTTPException(
                400, "scope must be one of: once, tool, glob")
        pattern = str(body.get("pattern") or "")
        if scope == "glob" and pattern and any(
                c in pattern for c in ("\n", "\r")):
            raise HTTPException(400, "pattern must be a single line")
        # NOT ``_require_live_session``: that also demands the live
        # graph handles, and this verb never touches the graph. The
        # parked lane is blocked inside its tool executor, so resolving
        # the request is the entire resume. Requiring ``_compiled``
        # here would 503 an ack that is perfectly answerable.
        s = _require_session(app, sid)
        if getattr(s, "closed", False):
            raise HTTPException(_HTTP_GONE, f"session closed: {sid}")
        req = s.tool_approvals.resolve(
            request_id, allow=allow,
            by=str(body.get("reason") or "assistant"),
            scope=scope, pattern=pattern,
        )
        s.bump_activity()
        if req is None:
            return {
                "ok": True, "resolved": False,
                "reason": "no matching pending approval request",
                "pending": s.tool_approvals.pending_public(),
                "grants": s.tool_approvals.grants_public(),
            }
        return {
            "ok": True, "resolved": True,
            "request": req.public_dict(),
            "pending": s.tool_approvals.pending_public(),
            "grants": s.tool_approvals.grants_public(),
        }

    # -------------------- POST /cancel ------------------------- #
    @app.post("/v1/consult/{sid}/cancel")
    def cancel(sid: str, body: Optional[dict] = None) -> dict:
        # Cancel is the one verb that MUST work even on completed/
        # failed sessions (idempotent no-op). For closed sessions
        # we still 410 because the session is gone from memory.
        body = body or {}
        s = _require_session(app, sid)
        if getattr(s, "closed", False):
            raise HTTPException(
                _HTTP_GONE, f"session closed: {sid}",
            )
        discard = bool(body.get("discard_partial") or False)
        reason = str(body.get("reason") or "user-cancel")
        req = build_cancel_request(
            discard_partial=discard, reason=reason,
        )
        # The state delta is the *record* of the request. It is not
        # what stops the run, and the difference is the whole reason
        # cancel was a no-op until 2026-08-02: a graph already inside
        # ``invoke`` carries its channels in memory for the superstep,
        # so the checkpoint ``update_state`` writes is never re-read. A
        # node consulting ``runtime_control.cancel_requested`` would
        # have seen False for the entire run.
        #
        # What actually stops it is ``run_control`` — a plain threading
        # object on the SessionState that the node gate reads directly,
        # the same out-of-band shape as the adversary ack and the
        # tool-approval broker. Every remaining node becomes a no-op
        # and the graph drains to END, keeping the partial state that
        # ``--keep-partial`` exists to preserve.
        if (getattr(s, "status", "") == "running"
                and getattr(s, "_compiled", None) is not None
                and getattr(s, "_thread_config", None) is not None):
            _safe_apply_state_delta(s, req.state_delta)
        running = getattr(s, "status", "") == "running"
        cancelled_now = s.run_control.request_cancel(reason)
        # M2: release the adversary checkpoint so the runner thread isn't
        # blocked for up to the full timeout on a session being
        # cancelled. The wait loop breaks on this ack (and on ``closed``
        # for the discard path below); the runner then re-streams to
        # completion — consistent with cancel-during-stream, which is
        # also cooperative (cancel_requested is advisory).
        if hasattr(s, "ack_adversary"):
            s.ack_adversary()
        # Discard the checkpoint file if asked. The checkpointer
        # factory uses per-session SQLite under the session dir; a
        # naive unlink is correct but only safe AFTER the runner
        # has released the file handle, which it does at stream
        # completion. Defer to the close path which already handles
        # the cleanup race.
        if discard and hasattr(app.state, "_close_session"):
            try:
                app.state._close_session(
                    app, sid, reason="cancel-discard",
                )
            except Exception:  # pragma: no cover — defensive
                log.exception("cancel-discard cleanup raised")
        s.bump_activity()
        return {
            "ok": True,
            "discard_partial": discard,
            "applied": _serialize_for_json(req.state_delta),
            # True once the node gate is wired: the remaining nodes
            # skip and the run drains. Still reported rather than
            # assumed — on a run that already finished there is
            # nothing left to stop, and "cancelled" should not read the
            # same as "asked to cancel".
            "stops_the_run": bool(discard or running),
            "cancel_accepted": bool(cancelled_now),
            # A cancelled run has no synthesized answer: the
            # synthesizer is a node like any other, and running it
            # would be spending after the caller said stop.
            "final_answer_expected": False if running else None,
        }

    # -------------------- GET /events (SSE) -------------------- #
    @app.get("/v1/consult/{sid}/events")
    async def events(sid: str, request: Request):
        # SSE — replay any persisted runtime_events the consumer
        # missed (Last-Event-ID), then poll the recorder for new
        # rows. The runner pumps custom events into the
        # runtime_events table; we don't subscribe to
        # ``astream_events`` directly (that would start a fresh
        # invocation). Polling cadence is 200ms — comfortably under
        # human-perceptible latency, comfortably above DB load
        # threshold for the SQLite recorder.
        s = _require_session(app, sid)
        last_event_id = _parse_last_event_id(
            request.headers.get("last-event-id"),
        )
        recorder = getattr(s, "_recorder", None)
        return StreamingResponse(
            _sse_stream_for_session(
                s, recorder, request,
                start_event_id=last_event_id,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # nginx
            },
        )


# ============================================================== #
# SSE stream loop
# ============================================================== #

async def _sse_stream_for_session(
    session,
    recorder,
    request,
    *,
    start_event_id: int = 0,
    poll_interval_s: float = 0.2,
    heartbeat_s: float = 15.0,
):
    """Async generator emitting SSE bytes for one session.

    Two-phase:

    1. **Replay** — emit every persisted ``runtime_events`` row with
       ``event_id > start_event_id``. Honors Last-Event-ID resume.
    2. **Tail** — poll the recorder every ``poll_interval_s`` for
       new rows; emit them as SSE; emit a comment-line heartbeat
       every ``heartbeat_s`` of quiet. End when the session reaches
       a terminal state (``completed`` / ``failed`` / closed) AND
       no new rows show up for one poll cycle.

    Client disconnect — FastAPI sets ``request.is_disconnected()``;
    we honor it by returning, which closes the generator and lets
    the StreamingResponse clean up.
    """
    from consultants.server.events_sse import (
        format_sse_event, format_sse_heartbeat,
    )

    last_id = int(start_event_id)
    last_emit_ts = time.monotonic()

    # Phase 1: replay
    if recorder is not None and hasattr(recorder, "list_runtime_events"):
        try:
            rows = recorder.list_runtime_events(since_event_id=last_id)
        except Exception:  # pragma: no cover — defensive
            rows = []
        for row in rows:
            eid = int(row.get("event_id") or 0)
            if eid <= last_id:
                continue
            yield format_sse_event(
                event_id=eid,
                event_type=str(row.get("kind") or "custom"),
                data=dict(row.get("payload") or {}),
            )
            last_id = eid
            last_emit_ts = time.monotonic()

    # Phase 2: tail
    while True:
        # Disconnect detection — FastAPI returns True once the TCP
        # connection has closed. Quick exit on disconnect avoids
        # keeping the generator + executor task alive.
        try:
            disconnected = await request.is_disconnected()
        except Exception:  # pragma: no cover — older fastapi
            disconnected = False
        if disconnected:
            return

        # Read any new rows.
        new_rows: list[dict] = []
        if recorder is not None and hasattr(recorder, "list_runtime_events"):
            try:
                new_rows = recorder.list_runtime_events(since_event_id=last_id)
            except Exception:  # pragma: no cover
                new_rows = []
        for row in new_rows:
            eid = int(row.get("event_id") or 0)
            if eid <= last_id:
                continue
            yield format_sse_event(
                event_id=eid,
                event_type=str(row.get("kind") or "custom"),
                data=dict(row.get("payload") or {}),
            )
            last_id = eid
            last_emit_ts = time.monotonic()

        # Heartbeat when quiet.
        if (time.monotonic() - last_emit_ts) >= heartbeat_s:
            yield format_sse_heartbeat()
            last_emit_ts = time.monotonic()

        # Termination check.
        if (getattr(session, "status", "") in ("completed", "failed")
                or getattr(session, "closed", False)):
            # One final poll already happened above. If no new rows,
            # this is the last lap — emit an explicit, durable
            # ``complete`` event so the consumer is NOTIFIED instead of
            # having to poll /state to discover the council finished.
            if not new_rows:
                status = (
                    getattr(session, "status", "")
                    or ("closed" if getattr(session, "closed", False)
                        else "unknown")
                )
                final_present = bool(
                    (getattr(session, "final_answer", "") or "").strip()
                )
                yield format_sse_event(
                    event_id=last_id,
                    event_type="complete",
                    data={
                        "sid": getattr(session, "sid", ""),
                        "status": status,
                        "final_answer_present": final_present,
                    },
                )
                return

        await asyncio.sleep(poll_interval_s)


# ============================================================== #
# Small helpers
# ============================================================== #

def _parse_last_event_id(raw: Optional[str]) -> int:
    """Parse the SSE ``Last-Event-ID`` header to an int. Returns 0
    when missing or unparseable — that's the "start from the
    beginning" sentinel."""
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _serialize_for_json(obj: Any) -> Any:
    """Best-effort dict-ification for arbitrary control objects.

    The builders return native dicts plus a couple of small
    dataclasses (Doc, ToolPlanItem). FastAPI's default JSON
    encoder handles dicts/lists/scalars but chokes on dataclasses;
    this normalizes them recursively.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _serialize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_for_json(v) for v in obj]
    # Dataclasses + named tuples + simple objects with __dict__.
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return _serialize_for_json(dict(d))
    # Fallback — repr it so we never crash the JSON encoder.
    return repr(obj)
