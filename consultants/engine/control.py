"""RuntimeControl defaults + accessor helpers.

The v2 council's per-session mutable knobs (deadlines, caps, quality
thresholds) live in ``state["runtime_control"]`` as a ``RuntimeControl``
TypedDict, reduced with a shallow merge so ``graph.update_state(...,
{"runtime_control": {...partial...}})`` does what callers expect.

This module owns:

- **Boot-time defaults** computed from the effort tier + fanout
  extras + the timing formula. Single source of truth — both the
  M2 node-side reads and the M9 HTTP control endpoint go through
  ``runtime_control_defaults``.

- **The timing formula** as data: per-effort ``(base_s, per_extra_s)``
  tuples + hard multiplier + per-lane hard cap. Grounded in the
  2026-05-16 analysis of csl-* session history (see
  ``/root/.claude/plans/recursive-petting-planet.md``).

- **Accessor helpers** node code calls at entry to read live values
  with v1-caps fallback:
  ``runtime_cap(state, "max_rounds", default=cfg_value)`` returns
  the RuntimeControl value when present, else the supplied default.

Pure-Python; imports cleanly without langgraph.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from consultants import config as cc
from consultants.engine.state_v2 import RuntimeControl


# ---------- timing formula data ---------------------------------- #
# Tuples are (base_s, per_extra_s) — see plan §"time-target formula".
# Grounded in 2026-05-16 historical analysis:
#   medium fanout averaged 71.1s synthesizer vs xhigh fanout 85.2s
#   (+20%) — x-tier base bumps account for the structural cost of
#   the critic+synthesizer reading N research reports even when
#   n_extras == 0. per_extra_s covers the additional cost per extra
#   model in fanout.

TIMING_BUDGETS: dict[str, tuple[int, int]] = {
    # base, per_extra
    "low":     (60,    0),
    "medium":  (180,   0),
    "high":    (480,   0),
    "max":     (900,   0),
    "xmedium": (240,  90),
    "xhigh":   (600, 180),
    "xmax":    (1080, 300),
    "xauto":   (720, 180),
}

HARD_MULTIPLIER: float = 3.0
HARD_MULTIPLIER_XAUTO: float = 4.0

# Per-lane absolute ceiling. Conservative initial value (60 min) —
# M11a bench produces model-specific tighter defaults. The M3 stall
# detector is the active mechanism that catches stalled lanes; this
# cap only fires when stall detection doesn't (a genuinely-stuck
# call with no token emission at all).
PER_LANE_HARD_S_DEFAULT: float = 3600.0

# Stall-detection defaults (M3 surfaces these knobs). Initial values
# kept loose; the M11a bench informs per-model tighter values.
DEFAULT_STALL_THRESHOLD_S: float = 300.0   # 5 min
DEFAULT_STALL_RETRIES: int = 1

# Synthesizer-self-rating cutoff for the xauto escalator. Below this
# triggers the next tier (M7).
DEFAULT_CONFIDENCE_TARGET: float = 0.7

# M4: map the static config ``adversary_strictness`` (soft|normal|strict)
# into the runtime critic-dial vocab (lax|normal|strict|adversarial) for
# the boot-time seed. ``adversarial`` is reachable only via a live
# POST /control mutation, never from the static seed.
_ADVERSARY_TO_CRITIC_STRICTNESS: dict[str, str] = {
    "soft": "lax",
    "normal": "normal",
    "strict": "strict",
}

# Effort -> (max_rounds, max_reroutes). Mirrors council.EFFORT_CAPS
# but normalized for x-tier inheritance. xauto starts modest and
# lets the escalator bump.
EFFORT_ROUND_CAPS: dict[str, tuple[int, int]] = {
    "low":     (1, 0),
    "medium":  (1, 1),
    "high":    (3, 2),
    "max":     (8, 5),
    "xmedium": (1, 1),
    "xhigh":   (3, 2),
    "xmax":    (8, 5),
    "xauto":   (1, 1),   # starts at the xmedium baseline
}


def time_target_for(effort: str,
                    n_fanout_extras: int) -> tuple[float, float]:
    """Return ``(soft_target_s, hard_s)`` for the effort + fanout
    breadth.

    ``soft_target_s`` is advisory — planner / researcher prompts see
    it as the recommended budget. ``hard_s`` is the absolute
    ceiling the deadline_ts is computed from. xauto uses a wider
    hard multiplier (4× vs 3×) because the escalator may grow the
    topology mid-session.
    """
    base, per_extra = TIMING_BUDGETS.get(
        effort, TIMING_BUDGETS["medium"],
    )
    soft = float(base + max(0, n_fanout_extras) * per_extra)
    mult = HARD_MULTIPLIER_XAUTO if effort == "xauto" else HARD_MULTIPLIER
    hard = soft * mult
    return (soft, hard)


def runtime_control_defaults(
    cfg: cc.ConsultantsConfig,
    *,
    effort: str,
    n_fanout_extras: int = 0,
    now_ts: Optional[float] = None,
) -> RuntimeControl:
    """Compute the boot-time RuntimeControl for a new consultation.

    Pure function — the runner calls this once at session start; the
    HTTP /control endpoint mutates the resulting record on demand
    via ``graph.update_state``. The merge reducer keeps unmentioned
    fields, so partial mutations are forgiving.

    ``n_fanout_extras`` is the number of *additional* models for the
    researcher fanout (0 for non-x tiers, ``len(extra_models)`` at x
    tiers). It feeds the timing formula's ``per_extra_s`` term.
    """
    if now_ts is None:
        now_ts = time.time()

    soft, hard = time_target_for(effort, n_fanout_extras)
    max_rounds, max_reroutes = EFFORT_ROUND_CAPS.get(
        effort, EFFORT_ROUND_CAPS["medium"],
    )

    enabled = list(cc.enabled_roles(cfg))

    # xauto bookkeeping: this tier starts at xmedium and the
    # escalator advances it; non-xauto tiers record themselves
    # so callers can introspect uniformly.
    if effort == "xauto":
        starting_tier = "xmedium"
    elif effort.startswith("x") and effort[1:] in (
            "medium", "high", "max"):
        starting_tier = effort
    else:
        # base-tier; xauto_tier is a meaningless field here. Pick a
        # sentinel value the escalator ignores.
        starting_tier = "xmedium"

    # M4: the boot-time critic dial is seeded from the static config
    # ``adversary_strictness`` (M1), mapped into the critic vocab:
    # soft→lax, normal→normal, strict→strict. The default
    # ``normal``→``normal`` keeps cohort-2 parity; an operator who sets
    # adversary_strictness=soft|strict shifts the boot-time critic dial
    # too (the M1 "tunes both the critic dial and the adversary role"
    # contract). POST /control can override ``critic_strictness`` live
    # — including to the runtime-only ``adversarial`` level.
    seed_strictness = _ADVERSARY_TO_CRITIC_STRICTNESS.get(
        getattr(cfg, "adversary_strictness", "normal"), "normal")

    rc: RuntimeControl = {
        "deadline_ts": now_ts + hard,
        "soft_target_ts": now_ts + soft,
        "per_lane_hard_s": PER_LANE_HARD_S_DEFAULT,
        "max_rounds": max_rounds,
        "max_reroutes": max_reroutes,
        "enabled_roles": enabled,
        "confidence_target": DEFAULT_CONFIDENCE_TARGET,
        "critic_strictness": seed_strictness,
        "stall_threshold_s": DEFAULT_STALL_THRESHOLD_S,
        "stall_retries": DEFAULT_STALL_RETRIES,
        "tool_permissions": {},
        "xauto_tier": starting_tier,  # type: ignore[typeddict-item]
        "xauto_escalations": 0,
    }
    return rc


# ---------- accessor helpers (node-side) ------------------------- #

def runtime_get(state: dict, key: str, default: Any = None) -> Any:
    """Read ``state["runtime_control"][key]`` with a default.

    Used by node code that wants the live value but has a v1 cap
    fallback. Example::

        rounds_cap = runtime_get(state, "max_rounds",
                                 default=caps.researcher_rounds_max)
    """
    rc = state.get("runtime_control") or {}
    if key in rc and rc[key] is not None:
        return rc[key]
    return default


def runtime_max_rounds(state: dict, *,
                       fallback: int) -> int:
    """Return the live researcher-round cap. ``fallback`` is the v1
    ``caps_for(effort).researcher_rounds_max`` value — passed in by
    the caller so this helper stays config-independent.
    """
    return int(runtime_get(state, "max_rounds", default=fallback))


def runtime_max_reroutes(state: dict, *,
                         fallback: int) -> int:
    """Return the live critic-reroute cap. ``fallback`` is the v1
    ``caps_for(effort).critic_reroutes_max`` value."""
    return int(runtime_get(state, "max_reroutes", default=fallback))


def runtime_deadline_passed(state: dict,
                            *,
                            now_ts: Optional[float] = None) -> bool:
    """True iff ``deadline_ts`` is set AND we're past it.

    Nodes check this at entry to decide whether to short-circuit
    with a tombstone instead of paying another LLM call. Returns
    False when no deadline is configured (base-tier runs without
    runtime_control wired through).
    """
    rc = state.get("runtime_control") or {}
    deadline = rc.get("deadline_ts")
    if not deadline:
        return False
    if now_ts is None:
        now_ts = time.time()
    return float(deadline) < float(now_ts)


def runtime_enabled_roles(state: dict, *,
                          fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Return the live ``enabled_roles`` tuple. Used by the fanout
    dispatcher so mid-flight mutation of ``runtime_control.enabled_roles``
    actually changes who runs next round.

    ``fallback`` is the boot-time ``deps.enabled_roles`` — used when
    runtime_control is absent (legacy v1 path with no control wiring).
    """
    rc = state.get("runtime_control") or {}
    live = rc.get("enabled_roles")
    if live is None:
        return tuple(fallback)
    return tuple(live)


def runtime_critic_strictness(state: dict, *,
                              fallback: str = "normal") -> str:
    """Critic-strictness mode: 'lax' | 'normal' | 'strict' |
    'adversarial' (M4).

    The critic + meta-critic nodes interpolate this into their system
    prompt so a live POST /control mutation actually changes the next
    critic call's behavior. 'normal' is the default and contributes NO
    directive — the prompt is byte-identical to v1 (cohort-2 parity).
    """
    return str(runtime_get(state, "critic_strictness",
                           default=fallback))


def runtime_adversarial_focus(state: dict, *, fallback: str = "") -> str:
    """M4: an optional free-text attack brief the assistant injects via
    POST /control (``adversarial_focus``) to direct the critic/meta-
    critic at a specific weakness this question is vulnerable to. Empty
    by default → no focus block in the prompt (parity)."""
    val = runtime_get(state, "adversarial_focus", default=fallback)
    return str(val) if val is not None else ""
