"""``xauto`` adaptive-effort escalator.

The ``xauto`` effort tier starts at the ``xmedium`` topology + caps
and grows toward ``xhigh`` / ``xmax`` mid-flight when the council
needs more compute. Escalation is driven by observable state signals
(synthesizer self-rated confidence, critic verdict, time pressure,
critic-named gaps) rather than user opt-in — the consultation
discovers it's harder than expected and dials itself up.

Design constraints (per plan §M7):

- **Each transition fires at most once per consultation.** A
  consultation can walk ``xmedium → xhigh → xmax``, but never
  ``xmedium → xhigh → xmedium → xhigh`` (would burn token budget
  on oscillation, and the trace data shows escalation signals are
  monotonic — once a question reveals it's hard, it stays hard).
- **Hard ceiling at ``xmax`` + the xauto time budget.** Past
  ``xmax`` further escalation would require ``researcher.extras``
  beyond what the user configured (we don't synthesize new models
  on the fly); the synthesizer composes a best-effort answer with
  the confidence stamp documented in M5.
- **No escalation past 70% of the soft time target.** If we're
  already 70% through the budget, adding a critic + reroute would
  push the wall time past the soft target by the time it completes
  — better to ride out the current topology and let the synthesizer
  produce a labeled-uncertainty answer.
- **Pure-Python decision; mutation via state delta.** The decision
  function reads state and returns an
  :class:`EscalationDecision` (or ``None``). A separate ``apply_*``
  helper turns that into the state delta + runtime mutation. The
  graph caller emits the ``RuntimeMutation`` event so the SSE
  consumer sees the escalation live.

Pure-Python; no LangGraph import. Tests pass plain dicts as state.

The integration point (M7b) is a graph-conditional after the
critic node: it inspects state via :func:`next_escalation` and,
when a decision is returned, applies the delta + reroutes back
through the relevant node to consume the bigger topology.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from consultants.engine.state_v2 import (
    latest_confidence,
)

log = logging.getLogger("consultants.engine.escalation")


# ============================================================== #
# Signals + transitions
# ============================================================== #

EscalationSignal = Literal[
    "low_confidence",     # synthesizer self-rating < threshold
    "critic_dissent",     # critic returned needs_more_research
    "time_pressure",      # wall time approaching soft target
    "gap_named",          # critic flagged a specific gap
]


# Stages of the xauto state machine. The "current" stage lives in
# ``state["runtime_control"]["xauto_tier"]`` (M2 reserved this
# field). Defaults to ``"xmedium"`` on initial xauto runs.
XautoTier = Literal["xmedium", "xhigh", "xmax", "ceiling"]


# Permitted forward transitions. Each can fire at most once per
# consultation; the escalator's bookkeeping in
# ``runtime_control.xauto_escalations`` enforces this.
ALLOWED_TRANSITIONS: dict[XautoTier, XautoTier] = {
    "xmedium": "xhigh",
    "xhigh":   "xmax",
    "xmax":    "ceiling",
}


@dataclass(frozen=True)
class EscalationDecision:
    """The escalator's verdict.

    ``from_tier`` → ``to_tier`` is the proposed transition (e.g.
    ``"xmedium" → "xhigh"``). ``signal`` names why; ``reason`` is
    a one-line human-readable string the ``RuntimeMutation`` event
    surfaces ("confidence 0.42 < 0.70 after round 1").
    ``runtime_control_delta`` is the dict the graph applies via
    ``graph.update_state(..., {"runtime_control": delta})``; it
    holds the mutable knobs the new tier needs (more rounds, more
    reroutes, multi-critic on/off, etc.).
    """
    from_tier: XautoTier
    to_tier: XautoTier
    signal: EscalationSignal
    reason: str
    runtime_control_delta: dict[str, Any] = field(default_factory=dict)


# ============================================================== #
# Tier → topology mapping
# ============================================================== #

@dataclass(frozen=True)
class TierTopology:
    """The mutable runtime-control knobs that distinguish one xauto
    stage from the next. The escalator's ``runtime_control_delta``
    is the diff between the from-tier's snapshot and the to-tier's.
    """
    max_rounds: int
    max_reroutes: int
    confidence_target: float
    # Multi-critic at xmax — the dispatcher consults this flag to
    # decide whether to fan critic across researcher.extra_models.
    # M7 only sets the flag; the graph wiring in M7b decides whether
    # to honor it (consistent with the existing
    # ``extras_active(effort)`` + ``deps.extra_models_by_role``
    # pattern).
    multi_critic: bool = False


# Snapshot of each stage's RuntimeControl values. Numbers are
# anchored to the existing EFFORT_CAPS in council.py:
# - xmedium  -> caps_for("medium")  = (rounds 1, reroutes 1)
# - xhigh    -> caps_for("high")    = (rounds 3, reroutes 2)
# - xmax     -> caps_for("max")     = (rounds 8, reroutes 5)
# confidence_target starts permissive at xmedium (so xauto doesn't
# escalate trivial questions) and tightens at higher tiers.
TIER_TOPOLOGIES: dict[XautoTier, TierTopology] = {
    "xmedium": TierTopology(
        max_rounds=1, max_reroutes=1,
        confidence_target=0.60, multi_critic=False,
    ),
    "xhigh": TierTopology(
        max_rounds=3, max_reroutes=2,
        confidence_target=0.70, multi_critic=False,
    ),
    "xmax": TierTopology(
        max_rounds=8, max_reroutes=5,
        confidence_target=0.75, multi_critic=True,
    ),
    # Ceiling is a sentinel — same topology as xmax. Escalating
    # *to* ceiling is a no-op from the topology perspective; it
    # exists so the state machine has a "we've already grown as
    # much as we can" stop state, distinct from never-yet-fired.
    "ceiling": TierTopology(
        max_rounds=8, max_reroutes=5,
        confidence_target=0.75, multi_critic=True,
    ),
}


# ============================================================== #
# Decision function (pure)
# ============================================================== #

def is_xauto_run(state: dict) -> bool:
    """True when this consultation is running under the xauto tier.

    Checked on every escalator call so non-xauto runs short-circuit
    immediately — keeps the cost on the hot path one dict read.
    """
    return (state.get("effort") or "").strip() == "xauto"


def current_tier(state: dict) -> XautoTier:
    """Return the active xauto tier from runtime_control, defaulting
    to ``"xmedium"`` for fresh runs that haven't escalated yet.

    Stored under ``runtime_control.xauto_tier`` (M2 reserved this
    field on the TypedDict so reducers know about it).
    """
    rc = state.get("runtime_control") or {}
    tier = rc.get("xauto_tier") or "xmedium"
    if tier not in ("xmedium", "xhigh", "xmax", "ceiling"):
        log.warning(
            "current_tier: unknown xauto_tier %r in runtime_control; "
            "defaulting to xmedium", tier,
        )
        return "xmedium"
    return tier  # type: ignore[return-value]


def _is_time_pressure(state: dict, *,
                     threshold: float = 0.70) -> bool:
    """True when ≥ ``threshold`` fraction of the soft-target budget
    has elapsed.

    Math anchors on three RuntimeControl fields:

    - ``started_ts`` — wall-clock time the consultation began.
      Set by ``runtime_control_defaults`` at session start.
    - ``soft_target_ts`` — wall-clock time the planner was told
      to aim for.
    - elapsed = now − started_ts; budget = soft_target − started.

    Returns False when ``started_ts`` or ``soft_target_ts`` is
    absent (no anchor → can't compute pressure conservatively).
    Returns False when budget ≤ 0 (defensive). Threshold default
    of 0.70 matches the plan §M7 spec.
    """
    rc = state.get("runtime_control") or {}
    started = rc.get("started_ts")
    soft = rc.get("soft_target_ts")
    if not started or not soft:
        return False
    import time
    now = time.time()
    budget = float(soft) - float(started)
    if budget <= 0:
        return False
    elapsed = now - float(started)
    return elapsed >= threshold * budget


def _critic_named_gap(state: dict) -> bool:
    """True when the critic's verdict text names a specific gap.

    Heuristic: critic_decision == "needs_more_research" AND the
    critique text contains a "gap_named:" marker OR a known
    English phrase indicating a named gap. Conservative — better
    to miss a signal than to fire on every reroute.
    """
    if (state.get("critic_decision") or "") != "needs_more_research":
        return False
    critique = (state.get("critique") or "").lower()
    if not critique:
        return False
    # Look for explicit markers first.
    if "gap_named:" in critique or "specific gap:" in critique:
        return True
    # Phrase heuristic: critic must name a concrete missing thing.
    # Each fragment must be specific enough that a generic
    # "needs more research" verdict doesn't trigger.
    for phrase in (
        "specifically missing",
        "concrete missing fact",
        "no evidence for",
        "could not verify",
        "no source for",
    ):
        if phrase in critique:
            return True
    return False


def next_escalation(
    state: dict,
    *,
    min_round_for_escalation: int = 1,
) -> Optional[EscalationDecision]:
    """Decide whether the council should escalate now. Returns
    ``None`` when no transition is warranted, or an
    :class:`EscalationDecision` describing the proposed change.

    Pre-conditions (any failure returns None silently):

    - Run is xauto (``state["effort"] == "xauto"``).
    - Current tier has a forward transition (not already at
      ``"ceiling"``).
    - This transition hasn't already fired (tracked by
      comparing current_tier vs the from-tier; one-shot per pair).
    - At least ``min_round_for_escalation`` researcher rounds have
      completed (default 1 — escalating before any evidence is
      gathered is premature).
    - Not under critical time pressure (≥ 70% of soft target
      elapsed); past that, escalating costs more than the
      remaining budget can absorb.

    Signal priority (first match wins, most-specific first):

    1. ``gap_named`` — critic named a specific missing fact (the
       strongest signal — we know precisely what to investigate
       next, so the larger topology has a concrete target).
    2. ``critic_dissent`` — generic ``needs_more_research``
       without a named gap.
    3. ``low_confidence`` — synthesizer self-rated below tier
       target.
    4. ``time_pressure`` — wall time approaching soft target with
       no critic yet enabled (rare upgrade path; the 70% guard
       suppresses most time_pressure triggers).

    The first three SUPPRESS when ``_is_time_pressure(state)``
    is True (we're already past 70% of the soft budget; growing
    the topology would push us over the cliff). The fourth is
    the carve-out: time pressure ONLY upgrades when we'd
    otherwise have no critic to weigh in.
    """
    if not is_xauto_run(state):
        return None
    cur = current_tier(state)
    target = ALLOWED_TRANSITIONS.get(cur)
    if target is None or target == "ceiling":
        return None  # Already at ceiling; no further forward
                     # transitions available.
    rounds_used = int(state.get("research_rounds_used") or 0)
    if rounds_used < min_round_for_escalation:
        return None  # Premature — give the council at least one
                     # round before judging it needs more.
    # Time-pressure suppression vs trigger: under_pressure
    # SUPPRESSES the generic signals (cost > remaining budget),
    # but it's the trigger for the time_pressure carve-out below.
    under_pressure = _is_time_pressure(state)

    # Signal 1: critic-named gap (most specific — beats generic
    # dissent because we know exactly what's missing).
    if _critic_named_gap(state) and not under_pressure:
        return _decision(cur, target, "gap_named",
                         reason="critic named a specific missing "
                                f"fact after round {rounds_used}")

    # Signal 2: generic critic dissent.
    if (state.get("critic_decision") or "") == "needs_more_research" \
            and not under_pressure:
        return _decision(cur, target, "critic_dissent",
                         reason="critic returned needs_more_research "
                                f"after round {rounds_used}")

    # Signal 3: low confidence. Read against the *current tier's*
    # target — gradual tightening as we escalate.
    cur_topo = TIER_TOPOLOGIES[cur]
    score = latest_confidence(state)
    if score is not None and score < cur_topo.confidence_target \
            and not under_pressure:
        return _decision(
            cur, target, "low_confidence",
            reason=f"synthesizer self-rated {score:.2f} < "
                   f"{cur_topo.confidence_target:.2f} (tier {cur})",
        )

    # Signal 4: time pressure as a reason to upgrade (the rare
    # case — usually time pressure SUPPRESSES escalation). Fires
    # ONLY when:
    # - we're under pressure (≥ 70% of soft budget elapsed)
    # - the next tier is xhigh (i.e. we're at xmedium today)
    # - no critic has run yet (critic_decision is absent — implies
    #   no critic in the active topology). If a critic ran, we'd
    #   already have a critic_decision and one of signals 1-3 would
    #   have fired or stayed silent on its own.
    # - explicit enabled_roles confirms critic isn't wired (defends
    #   against a state where critic ran via a different path).
    enabled_roles = (
        state.get("runtime_control", {}).get("enabled_roles") or []
    )
    if (under_pressure
            and target == "xhigh"
            and not state.get("critic_decision")
            and "critic" not in enabled_roles):
        return _decision(cur, target, "time_pressure",
                         reason="approaching soft target; "
                                "promoting to xhigh for critic review")

    return None


def _decision(
    from_tier: XautoTier,
    to_tier: XautoTier,
    signal: EscalationSignal,
    *,
    reason: str,
) -> EscalationDecision:
    """Build an :class:`EscalationDecision` for ``from_tier`` →
    ``to_tier`` with the topology delta auto-computed from the
    TIER_TOPOLOGIES table.

    The delta is the keys that changed between the two tiers'
    TierTopology values, PLUS the ``xauto_tier`` field so the
    next ``current_tier(state)`` call returns the new stage.
    ``xauto_escalations`` increments so post-mortems can count
    how many times the council grew during this consultation.
    """
    a = TIER_TOPOLOGIES[from_tier]
    b = TIER_TOPOLOGIES[to_tier]
    delta: dict[str, Any] = {
        "xauto_tier": to_tier,
    }
    # Only emit fields that actually changed.
    if b.max_rounds != a.max_rounds:
        delta["max_rounds"] = b.max_rounds
    if b.max_reroutes != a.max_reroutes:
        delta["max_reroutes"] = b.max_reroutes
    if b.confidence_target != a.confidence_target:
        delta["confidence_target"] = b.confidence_target
    # multi_critic isn't a RuntimeControl key directly — the graph
    # builder reads it from runtime_control to decide whether to
    # wire the critic fanout. We surface it on the delta so the
    # graph integration in M7b sees it.
    if b.multi_critic != a.multi_critic:
        delta["multi_critic"] = b.multi_critic
    return EscalationDecision(
        from_tier=from_tier, to_tier=to_tier,
        signal=signal, reason=reason,
        runtime_control_delta=delta,
    )


def apply_escalation(
    state: dict,
    decision: EscalationDecision,
) -> dict:
    """Build the state delta the graph caller hands to
    ``graph.update_state`` (or returns from a node).

    Bumps ``runtime_control.xauto_escalations`` so post-mortems
    can count growth events, and increments any state-level
    counters tied to the new tier. The caller is responsible for
    emitting the ``RuntimeMutation`` event — that's a streaming
    concern, separate from the durable state change.

    Returns shape::

        {"runtime_control": {
             "xauto_tier": "xhigh",
             "max_rounds": 3,
             ...
             "xauto_escalations": <prev + 1>,
        }}
    """
    rc = state.get("runtime_control") or {}
    prev_escalations = int(rc.get("xauto_escalations") or 0)
    out: dict[str, Any] = {
        "runtime_control": dict(decision.runtime_control_delta),
    }
    out["runtime_control"]["xauto_escalations"] = prev_escalations + 1
    return out


def runtime_mutation_event_data(decision: EscalationDecision) -> dict:
    """Build the payload for the ``RuntimeMutation`` event the
    streaming consumer (SSE / live dashboard) sees alongside the
    state change.

    The event's ``changes`` field is the delta keys only (not the
    full RuntimeControl snapshot — that would bloat the stream).
    ``reason`` is the human-readable string the live UI renders as
    "xauto escalation: xmedium -> xhigh — confidence 0.42 < 0.60".
    """
    return {
        "changes": dict(decision.runtime_control_delta),
        "reason": (
            f"xauto escalation: {decision.from_tier} → "
            f"{decision.to_tier} ({decision.signal}) — "
            f"{decision.reason}"
        ),
    }


__all__ = [
    "ALLOWED_TRANSITIONS",
    "EscalationDecision",
    "EscalationSignal",
    "TIER_TOPOLOGIES",
    "TierTopology",
    "XautoTier",
    "apply_escalation",
    "current_tier",
    "is_xauto_run",
    "next_escalation",
    "runtime_mutation_event_data",
]
