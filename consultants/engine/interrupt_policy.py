"""Centralized HITL (human-in-the-loop) interrupt-policy decisions.

.. warning::

   **AUDIT 2026-08-02 — none of the four policies below has a caller.**
   Every ``should_interrupt_*`` function is exercised only by its unit
   tests; no node in ``consultants/engine/`` consults one, and nothing
   reads ``runtime_control.pause_requested`` or ``cancel_requested``.
   The prose below describes a design, not the running system. Two of
   the four decision points were since solved a different way and the
   difference is deliberate, not an oversight:

   * *Tool permission* (#3) is handled by
     :mod:`consultants.engine.tool_approval`, which parks the calling
     lane inside its tool executor rather than interrupting the graph.
     A graph-level interrupt would idle every x-tier sibling to wait on
     one lane's write. See the SUPERSEDED note on
     :func:`should_interrupt_on_tool_permission`.
   * The *adversary checkpoint* uses the same park-poll-deadline shape
     in the runner (``_await_adversary_ack``).

   #1, #2 and #4 remain unimplemented: ``review_before_synthesis``,
   the low-confidence pause, and ``POST /interrupt`` are advisory —
   they record a request that no node acts on. Wire them or delete
   them; leaving them looking implemented is how ``/cancel`` came to
   carry a comment asserting behaviour that did not exist.

The actual ``interrupt()`` call from ``langgraph.types`` is per-node
(it has to be — only the node knows what payload to pose to the
user), but **whether to call it** is a policy question that benefits
from one source of truth. That's this module.

Four decision points are covered:

1. **Static review-before-synthesis** — the user opted into
   ``cfg.runtime.review_before_synthesis``: before the synthesizer
   composes, pause and show the partial state for human approval.
2. **Dynamic low-confidence** — synthesizer's self-rating fell below
   the threshold AND the user has opted into the policy. Lets the
   user nudge the council ("add this context", "drop the GDPR
   lane", ...) instead of accepting a low-confidence answer.
3. **Tool permission "ask"** — a tool the researcher or
   tool_executor (M6) wants to call has its permission set to
   ``"ask"`` in ``runtime_control.tool_permissions``. Pause at the
   call site, surface the args, wait for human approval.
4. **User-initiated pause** — the HTTP control surface set
   ``runtime_control.pause_requested = True``. The next node to
   enter honors it cooperatively.

Each decision returns an ``InterruptDecision`` dataclass with the
``kind`` (used as the SSE event discriminator + the
``InterruptState.kind`` literal), the ``prompt`` (one-line summary
shown to the human), and the ``payload`` (full snapshot the
consumer needs). Callers serialize it via ``LangGraph.interrupt()``
which the durable runtime pickles into the checkpointer.

Pure-Python, no LangGraph import. The node code does::

    decision = should_interrupt_before_synthesis(state, cfg=cfg)
    if decision is not None:
        # langgraph.types.interrupt — lazy import at the call site
        from langgraph.types import interrupt
        answer = interrupt(decision.to_payload())
        # ``answer`` is whatever ``Command(resume=...)`` posted

so the policy file itself stays importable in the main
``claude-hooks`` test env without the langgraph dep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from consultants.engine.state_v2 import (
    InterruptState,
    latest_confidence,
)


# ============================================================== #
# The decision dataclass
# ============================================================== #

InterruptKind = Literal[
    "review",            # static review-before-synthesis
    "low_confidence",    # dynamic confidence-threshold trigger
    "tool_permission",   # tool with permission="ask"
    "user_pause",        # HTTP /interrupt → cooperative pause
]


@dataclass(frozen=True)
class InterruptDecision:
    """One interrupt-policy verdict.

    ``kind`` is the SSE event discriminator + ``InterruptState.kind``
    literal. ``prompt`` is the one-line summary the consumer renders
    ("Review draft before synthesis", "Confidence 0.42 < 0.70 —
    proceed?", ...). ``payload`` is the full snapshot the human needs
    to make the call: for ``review`` it's the partial synthesis; for
    ``tool_permission`` it's the tool name + args; for ``user_pause``
    it's a minimal state summary.

    ``urgent`` lets a future xauto policy escalate an interrupt past
    a deferred-batch optimization (not used in M5; reserved).
    """
    kind: InterruptKind
    prompt: str
    payload: dict[str, Any] = field(default_factory=dict)
    urgent: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Shape passed to ``langgraph.types.interrupt(...)``.

        The runtime serializes this into the checkpointer and
        surfaces it on ``state.tasks[i].interrupts[0].value`` for
        consumers fetching via ``GET /state``. Keeping the shape
        explicit (kind / prompt / payload) gives the consumer a
        stable contract independent of the LangGraph version.
        """
        return {
            "kind": self.kind,
            "prompt": self.prompt,
            "payload": dict(self.payload),
            "urgent": self.urgent,
        }

    def to_interrupt_state(self, *, posted_at: float) -> InterruptState:
        """Materialize the ``InterruptState`` channel value the node
        writes alongside the ``interrupt()`` call.

        The dual write — ``interrupt()`` for LangGraph's HITL
        machinery + ``state["interrupt_state"] = ...`` for the
        ``GET /state`` server endpoint — keeps the two consumers
        (the resume-with-Command path and the read-state-while-paused
        path) in sync without one having to scrape the other.
        """
        return InterruptState(
            kind=self.kind,  # type: ignore[arg-type]
            prompt=self.prompt,
            posted_at=posted_at,
            payload=dict(self.payload),
        )


# ============================================================== #
# Policy decision functions (pure)
# ============================================================== #

def should_interrupt_before_synthesis(
    state: dict,
    *,
    cfg: Optional[Any] = None,
) -> Optional[InterruptDecision]:
    """Static review-before-synthesis check.

    Fires when either:

    - ``cfg.runtime.review_before_synthesis`` is ``True`` (the user
      opted into review at session start), OR
    - ``state["runtime_control"]["review_before_synthesis"]`` is
      truthy (the HTTP control endpoint toggled it mid-flight).

    Returns ``None`` if the static interrupt has already fired and
    been resumed (``state["interrupt_state"] is None`` after a
    Command(resume=...) cleared it). The graph builder's
    ``interrupt_before=["synthesizer"]`` static gate is what actually
    pauses execution; this function is the read-side helper that
    builds the matching payload + lets the synthesizer node distinguish
    "first entry, must interrupt" from "post-resume re-entry, must
    synthesize".

    Pure read; never mutates state.
    """
    # Already paused or already resumed? Don't re-fire.
    if state.get("interrupt_state") is not None:
        return None
    rc = state.get("runtime_control") or {}
    review_flag = bool(rc.get("review_before_synthesis"))
    cfg_flag = False
    if cfg is not None:
        runtime = getattr(cfg, "runtime", None)
        if runtime is not None:
            cfg_flag = bool(getattr(runtime, "review_before_synthesis", False))
    if not (review_flag or cfg_flag):
        return None
    partial = state.get("partial_synthesis") or ""
    research_n = len(state.get("research") or [])
    return InterruptDecision(
        kind="review",
        prompt=(
            "Review before synthesis: "
            f"{research_n} research report(s) ready. Approve to "
            "compose the final answer, or inject more context."
        ),
        payload={
            "partial_synthesis": partial,
            "research_count": research_n,
            "plan_excerpt": (state.get("plan") or "")[:500],
            "critic_decision": state.get("critic_decision"),
        },
    )


def should_interrupt_on_low_confidence(
    state: dict,
    *,
    threshold: Optional[float] = None,
) -> Optional[InterruptDecision]:
    """Dynamic low-confidence trigger.

    Fires when:

    - The synthesizer has emitted at least one confidence score
      (``latest_confidence(state)`` is not None), AND
    - That score is below the active threshold, AND
    - ``runtime_control.interrupt_on_low_confidence`` is truthy
      (off by default — confidence < threshold normally drives
      xauto escalation, not a human interrupt).

    The active threshold is the explicit ``threshold`` arg if given,
    else ``runtime_control.confidence_target`` if set, else ``0.5``.
    The pre-conditions above are conservative: an empty confidence
    series never fires (no false positives during the first round)
    and the policy stays opt-in.
    """
    rc = state.get("runtime_control") or {}
    if not bool(rc.get("interrupt_on_low_confidence")):
        return None
    score = latest_confidence(state)
    if score is None:
        return None
    if threshold is None:
        threshold = float(rc.get("confidence_target") or 0.5)
    if score >= threshold:
        return None
    return InterruptDecision(
        kind="low_confidence",
        prompt=(
            f"Confidence {score:.2f} < {threshold:.2f}. "
            "Proceed anyway, inject context, or abort?"
        ),
        payload={
            "score": score,
            "threshold": threshold,
            "partial_synthesis": state.get("partial_synthesis") or "",
            "research_count": len(state.get("research") or []),
        },
    )


# SUPERSEDED 2026-08-02 — do not wire this.
#
# The plan (docs/PLAN-council-tool-surface.md, M-A) called for pausing
# "at the call site, surface the args, wait for human approval", and
# that is what shipped — but at the DISPATCH boundary, in
# ``consultants.engine.tool_approval``, not as a graph interrupt. The
# reasons are in that module: an ``ask_human`` there parks one lane by
# blocking its worker thread while its x-tier siblings keep running,
# which is the pause scope the plan decided on, and it costs no
# lane-scoped interrupt state in the checkpointer.
#
# This function is kept because its tests document the intended
# semantics, and deleting it would lose that. Wiring it as well would
# give one tool call two approval paths that can disagree.
def should_interrupt_on_tool_permission(
    state: dict,
    tool_name: str,
    *,
    args_preview: str = "",
) -> Optional[InterruptDecision]:
    """Per-tool permission check.

    Reads ``runtime_control.tool_permissions[tool_name]``:

    - ``"allow"`` (or missing) → no interrupt, tool runs.
    - ``"deny"`` → caller must skip the tool call; this function
      still returns ``None`` because the deny is enforced by the
      caller (different code path from an interrupt).
    - ``"ask"`` → interrupt with the tool name + args preview;
      consumer responds with a Command(resume={"approve": bool}).

    ``args_preview`` is a short (≤ 200 chars) truncation of the tool
    args — full args go on the recorder's event row.
    """
    rc = state.get("runtime_control") or {}
    perms = rc.get("tool_permissions") or {}
    perm = perms.get(tool_name)
    if perm != "ask":
        return None
    return InterruptDecision(
        kind="tool_permission",
        prompt=(
            f"Tool '{tool_name}' requires approval before running. "
            "Approve or deny?"
        ),
        payload={
            "tool": tool_name,
            "args_preview": args_preview[:200],
        },
        urgent=True,
    )


def should_interrupt_on_user_pause(
    state: dict,
    *,
    role: str = "",
) -> Optional[InterruptDecision]:
    """User-initiated cooperative pause.

    Fires when ``runtime_control.pause_requested`` is truthy AND
    no interrupt is currently active. The HTTP ``/interrupt``
    endpoint flips this flag; the next node entry honors it.

    ``role`` (planner / researcher / ...) is recorded on the
    payload so the consumer can show "paused before researcher
    round 2 (lane 3)" without scraping events.
    """
    rc = state.get("runtime_control") or {}
    if not bool(rc.get("pause_requested")):
        return None
    if state.get("interrupt_state") is not None:
        return None
    research_n = len(state.get("research") or [])
    return InterruptDecision(
        kind="user_pause",
        prompt=(
            f"Paused before {role or 'next role'}. "
            "Inject context, mutate runtime control, or resume."
        ),
        payload={
            "role": role,
            "research_count": research_n,
            "plan_excerpt": (state.get("plan") or "")[:500],
            "partial_synthesis": state.get("partial_synthesis") or "",
        },
    )


# ============================================================== #
# Resume / clear helpers
# ============================================================== #

def clear_interrupt(state: dict) -> dict:
    """Return a state-delta dict that clears the active interrupt.

    Nodes that consume the resume signal (the synthesizer post-review,
    the researcher post-tool-approval, ...) merge this into their
    return so the next node sees ``interrupt_state = None`` and the
    pause flag flipped off. Composed once here so call sites stay
    consistent.
    """
    return {
        "interrupt_state": None,
        # If a user_pause fired, clear the pause flag so subsequent
        # nodes don't re-interrupt. Tool/permission/low-confidence
        # don't set this flag, so the merge is a safe no-op.
        "runtime_control": {"pause_requested": False},
    }


__all__ = [
    "InterruptDecision",
    "InterruptKind",
    "clear_interrupt",
    "should_interrupt_before_synthesis",
    "should_interrupt_on_low_confidence",
    "should_interrupt_on_tool_permission",
    "should_interrupt_on_user_pause",
]
