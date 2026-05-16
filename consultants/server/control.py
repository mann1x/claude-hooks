"""Pure-Python builders for the v2 HTTP control surface.

The M9 FastAPI routes (``POST /v1/consult/<sid>/{inject,control,
interrupt,resume,cancel}``) are thin shells: they validate the JSON
body, call one of the functions below to build the matching
LangGraph operation, then dispatch it against the compiled graph.

By keeping the dispatch primitives pure (no FastAPI / no langgraph
imports at module top level), this module unit-tests cleanly on the
main ``claude-hooks`` env and the M9 work doesn't have to re-test
payload shapes — only the route plumbing.

Each builder returns one of:

- A **state delta** ``dict`` ready for
  ``graph.update_state(config, delta, as_node=<source>)``.
- An ``InterruptResume`` value object the M9 layer hands to
  ``Command(resume=...)`` (langgraph imported lazily there).
- A ``CancelRequest`` value object with the discard-partial flag.

The builders all validate inputs and raise ``ControlInputError`` on
bad shape — the FastAPI layer turns these into 400 responses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from consultants.engine.state_v2 import Doc


# ============================================================== #
# Errors
# ============================================================== #

class ControlInputError(ValueError):
    """Raised when a control-endpoint payload is malformed.

    The HTTP layer maps this to a 400 with the error string as the
    body — the message is user-facing, so keep it concise and
    actionable.
    """


# ============================================================== #
# Valid sentinels
# ============================================================== #

VALID_INJECT_ROLES: tuple[str, ...] = (
    "planner", "researcher", "critic", "synthesizer", "any",
)

VALID_TOOL_PERMISSION_VALUES: tuple[str, ...] = ("allow", "deny", "ask")

VALID_STRICTNESS_VALUES: tuple[str, ...] = ("lax", "normal", "strict")


# ============================================================== #
# Value objects returned to the M9 layer
# ============================================================== #

@dataclass(frozen=True)
class InterruptResume:
    """The shape of a Command(resume=...) payload the server hands
    back to LangGraph after a HITL pause.

    ``value`` is the consumer's response (typically a dict like
    ``{"approve": true}`` for tool_permission, the edited
    ``additional_context`` for review-before-synthesis, etc.).
    ``decision`` is a short string for telemetry (``"approve"``,
    ``"edit"``, ``"abort"``) that the recorder logs alongside the
    full payload.
    """
    value: Any
    decision: str = ""


@dataclass(frozen=True)
class CancelRequest:
    """The state delta + intent the server layer needs to cooperatively
    drain a running consultation.

    ``state_delta`` flips the runtime_control flag the nodes honor
    on entry; the M9 layer also issues a ``RunControl.request_drain``
    so the running asyncio task notices cooperatively rather than
    waiting for the next node boundary.

    ``discard_partial`` tells the server whether to delete the
    checkpoint on shutdown (clean slate) or keep it (so the user
    can inspect the partial state).
    """
    state_delta: dict[str, Any]
    discard_partial: bool


# ============================================================== #
# Builder: inject
# ============================================================== #

def build_inject_delta(
    *,
    role: str,
    text: str,
    source: str = "user",
    ts: Optional[float] = None,
) -> dict[str, Any]:
    """Build the ``graph.update_state`` delta for ``POST /inject``.

    Returns a dict shaped::

        {"additional_context": [Doc(role, text, ts, source)]}

    The reducer at the state level (``append_doc``) dedups on the
    Doc's ``content_hash`` so duplicate injects (retry storms, idempotent
    client behavior) are silently merged into one. ``role`` must be
    one of :data:`VALID_INJECT_ROLES`; ``text`` must be non-empty
    after strip().
    """
    if role not in VALID_INJECT_ROLES:
        raise ControlInputError(
            f"inject role must be one of {VALID_INJECT_ROLES}; "
            f"got {role!r}"
        )
    cleaned = (text or "").strip()
    if not cleaned:
        raise ControlInputError("inject text must be non-empty")
    if len(cleaned) > 50_000:
        raise ControlInputError(
            "inject text exceeds 50,000 chars; trim before sending"
        )
    doc = Doc(
        role=role,
        text=cleaned,
        ts=float(ts) if ts is not None else time.time(),
        source=source or "user",
    )
    return {"additional_context": [doc]}


# ============================================================== #
# Builder: control (RuntimeControl mutation)
# ============================================================== #

def build_runtime_control_delta(
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Build the delta for ``POST /control``.

    Validates each known key. Unknown keys are rejected (rather
    than silently dropped) so the caller knows their request didn't
    take effect. Recognized keys:

    - ``deadline_ts`` (float; absolute) — replaces the wall-clock
      ceiling. The HTTP layer typically passes ``time.time() + delta``
      computed from a ``"+30m"``-style argument.
    - ``soft_target_ts`` (float; absolute) — same shape as deadline.
    - ``max_rounds`` (int ≥ 0) — researcher round cap.
    - ``max_reroutes`` (int ≥ 0) — critic reroute cap.
    - ``confidence_target`` (float in [0, 1]) — synth self-rating cutoff.
    - ``critic_strictness`` (``"lax"`` / ``"normal"`` / ``"strict"``).
    - ``enabled_roles`` (list[str]) — subset of the project's roles.
    - ``tool_permissions`` (dict[str, "allow" / "deny" / "ask"]).
    - ``review_before_synthesis`` (bool) — toggle static interrupt.
    - ``interrupt_on_low_confidence`` (bool) — toggle dynamic interrupt.

    The returned shape::

        {"runtime_control": {<validated changes>}}

    is intentionally a partial — the state reducer
    (``merge_runtime_control``) deep-merges into the existing
    channel, so unrelated fields are preserved.
    """
    if not isinstance(changes, dict) or not changes:
        raise ControlInputError(
            "control body must be a non-empty object of "
            "RuntimeControl key->value updates"
        )
    out: dict[str, Any] = {}
    for k, v in changes.items():
        if k in ("deadline_ts", "soft_target_ts"):
            if not isinstance(v, (int, float)):
                raise ControlInputError(
                    f"{k} must be a float (absolute time.time())"
                )
            out[k] = float(v)
        elif k in ("per_lane_hard_s", "stall_threshold_s"):
            if not isinstance(v, (int, float)) or v <= 0:
                raise ControlInputError(
                    f"{k} must be a positive number of seconds"
                )
            out[k] = float(v)
        elif k in ("max_rounds", "max_reroutes", "stall_retries"):
            if not isinstance(v, int) or v < 0:
                raise ControlInputError(
                    f"{k} must be a non-negative integer"
                )
            out[k] = int(v)
        elif k == "confidence_target":
            if not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
                raise ControlInputError(
                    "confidence_target must be a float in [0, 1]"
                )
            out[k] = float(v)
        elif k == "critic_strictness":
            if v not in VALID_STRICTNESS_VALUES:
                raise ControlInputError(
                    f"critic_strictness must be one of "
                    f"{VALID_STRICTNESS_VALUES}"
                )
            out[k] = v
        elif k == "enabled_roles":
            if not isinstance(v, list) \
                    or not all(isinstance(r, str) for r in v):
                raise ControlInputError(
                    "enabled_roles must be a list of role names"
                )
            out[k] = list(v)
        elif k == "tool_permissions":
            if not isinstance(v, dict):
                raise ControlInputError(
                    "tool_permissions must be a dict of "
                    "tool_name -> 'allow'|'deny'|'ask'"
                )
            for tn, perm in v.items():
                if perm not in VALID_TOOL_PERMISSION_VALUES:
                    raise ControlInputError(
                        f"tool_permissions[{tn!r}] must be one of "
                        f"{VALID_TOOL_PERMISSION_VALUES}; got {perm!r}"
                    )
            out[k] = dict(v)
        elif k in ("review_before_synthesis",
                   "interrupt_on_low_confidence"):
            out[k] = bool(v)
        else:
            raise ControlInputError(
                f"unknown runtime_control key: {k!r}"
            )
    return {"runtime_control": out}


# ============================================================== #
# Builder: interrupt (cooperative pause request)
# ============================================================== #

def build_interrupt_delta(
    *,
    reason: str = "user-pause",
) -> dict[str, Any]:
    """Build the delta for ``POST /interrupt``.

    Doesn't actually pause anything — it just flips the
    ``runtime_control.pause_requested`` flag. The next node that
    enters consults :func:`consultants.engine.interrupt_policy
    .should_interrupt_on_user_pause` and, if the flag is set,
    calls ``langgraph.types.interrupt(...)`` which durably parks
    execution.

    ``reason`` is recorded on the runtime_mutation event so the
    transcript shows why the pause was requested.
    """
    reason_str = (reason or "user-pause").strip() or "user-pause"
    return {
        "runtime_control": {
            "pause_requested": True,
            "pause_reason": reason_str,
        }
    }


# ============================================================== #
# Builder: resume
# ============================================================== #

def build_resume_command(
    value: Any,
    *,
    decision: str = "",
) -> InterruptResume:
    """Build the ``Command(resume=...)`` payload for ``POST /resume``.

    ``value`` is whatever the consumer sent — for a static-review
    interrupt it's typically ``{"approve": True}`` or a dict of
    edits the synthesizer should consume; for tool-permission it's
    ``{"approve": True}`` / ``{"approve": False}``; for low-confidence
    it's similar.

    The M9 layer issues this against LangGraph as::

        graph.update_state(config, clear_interrupt({}),
                            as_node="resumer")
        graph.invoke(Command(resume=resume.value),
                     config={"configurable": {"thread_id": sid}})

    where the first update clears the ``interrupt_state`` channel
    so the next-node-entry guard in
    :func:`should_interrupt_before_synthesis` doesn't re-fire.
    """
    return InterruptResume(value=value, decision=decision or "")


# ============================================================== #
# Builder: cancel
# ============================================================== #

def build_cancel_request(
    *,
    discard_partial: bool = False,
    reason: str = "user-cancel",
) -> CancelRequest:
    """Build the cancel request for ``POST /cancel``.

    Sets ``runtime_control.cancel_requested = True`` on the state.
    Nodes honor it cooperatively at entry (they exit early with a
    tombstone return), and the server layer ALSO issues a
    ``RunControl.request_drain("user-cancel")`` so the running
    asyncio task drains promptly.

    ``discard_partial`` is forwarded to the cleanup path: when
    ``True``, the checkpointer file for the session is deleted; when
    ``False``, the checkpoint stays so the user can inspect what got
    built.
    """
    reason_str = (reason or "user-cancel").strip() or "user-cancel"
    return CancelRequest(
        state_delta={
            "runtime_control": {
                "cancel_requested": True,
                "cancel_reason": reason_str,
            }
        },
        discard_partial=bool(discard_partial),
    )


# ============================================================== #
# State-snapshot summarizer (for GET /state)
# ============================================================== #

def summarize_state_for_get(
    raw: dict[str, Any],
    *,
    sid: str,
) -> dict[str, Any]:
    """Turn a LangGraph ``StateSnapshot`` (or any dict-shaped state)
    into the user-facing ``GET /v1/consult/<sid>/state`` body.

    Drops bulky channels (full research reports, raw transcripts)
    and surfaces the high-signal fields a Claude / human watcher
    needs:

    - ``runtime_control`` — current mutable knobs.
    - ``interrupt_state`` — what (if anything) the council is
      waiting on.
    - ``latest_confidence`` + ``partial_synthesis`` — synth progress.
    - ``research_count`` + ``plan_items_count`` — progress hints
      without the bulk.
    - ``additional_context`` — full list (small per inject, capped
      by the 50K char limit).

    Pure function; no I/O.
    """
    state = raw.get("values") if "values" in raw else raw
    rc = (state.get("runtime_control") or {})
    interrupt_state = state.get("interrupt_state")
    research = state.get("research") or []
    plan_items = state.get("plan_items") or []
    confidence_series = state.get("confidence") or []
    latest = confidence_series[-1] if confidence_series else None
    return {
        "sid": sid,
        "runtime_control": dict(rc),
        "interrupt_state": (
            _interrupt_state_to_dict(interrupt_state)
            if interrupt_state is not None else None
        ),
        "research_count": len(research),
        "plan_items_count": len(plan_items),
        "research_rounds_used": int(state.get("research_rounds_used") or 0),
        "critic_reroutes_used": int(state.get("critic_reroutes_used") or 0),
        "critic_decision": state.get("critic_decision"),
        "latest_confidence": latest,
        "partial_synthesis": state.get("partial_synthesis"),
        "additional_context": [
            {
                "role": d.role,
                "text": d.text,
                "source": getattr(d, "source", "user"),
                "ts": d.ts,
            }
            for d in (state.get("additional_context") or [])
        ],
        "error": state.get("error"),
        "_role_failed": state.get("_role_failed"),
        "final_answer_ready": bool(
            (state.get("final_answer") or "").strip()
        ),
        # next + tasks lifted straight from LangGraph's snapshot if present.
        "next": raw.get("next"),
        "tasks": _tasks_summary(raw.get("tasks") or []),
    }


def _interrupt_state_to_dict(interrupt_state: Any) -> dict[str, Any]:
    """Tolerant InterruptState → dict converter.

    Handles both the dataclass (live channel) and a raw dict (in
    case a re-loaded checkpoint deserialized it as a dict).
    """
    if isinstance(interrupt_state, dict):
        return dict(interrupt_state)
    return {
        "kind": getattr(interrupt_state, "kind", None),
        "prompt": getattr(interrupt_state, "prompt", ""),
        "posted_at": getattr(interrupt_state, "posted_at", 0.0),
        "payload": dict(getattr(interrupt_state, "payload", {}) or {}),
    }


def _tasks_summary(tasks: list[Any]) -> list[dict[str, Any]]:
    """Compact list of pending tasks from LangGraph's StateSnapshot.

    Each task carries its name + interrupts (so the consumer can
    discover what's pending without parsing the raw langgraph
    objects).
    """
    out: list[dict[str, Any]] = []
    for t in tasks:
        if isinstance(t, dict):
            name = t.get("name") or ""
            interrupts = t.get("interrupts") or []
        else:
            name = getattr(t, "name", "") or ""
            interrupts = getattr(t, "interrupts", []) or []
        out.append({
            "name": str(name),
            "interrupts": [
                _interrupt_value(i) for i in interrupts
            ],
        })
    return out


def _interrupt_value(interrupt_obj: Any) -> dict[str, Any]:
    """Pull the value dict out of a LangGraph Interrupt object."""
    if isinstance(interrupt_obj, dict):
        return dict(interrupt_obj.get("value") or {})
    v = getattr(interrupt_obj, "value", None)
    if isinstance(v, dict):
        return dict(v)
    return {"value": str(v)} if v is not None else {}


__all__ = [
    "CancelRequest",
    "ControlInputError",
    "InterruptResume",
    "VALID_INJECT_ROLES",
    "VALID_STRICTNESS_VALUES",
    "VALID_TOOL_PERMISSION_VALUES",
    "build_cancel_request",
    "build_inject_delta",
    "build_interrupt_delta",
    "build_resume_command",
    "build_runtime_control_delta",
    "summarize_state_for_get",
]
