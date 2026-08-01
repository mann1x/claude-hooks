"""Per-model stall-detection defaults (M11a scaffold).

The values here are the **recommended** per-model overrides for
``stall_threshold_s`` and ``hard_cap_s`` — the two thresholds the
M3 ``StallMonitor`` consults to decide whether an in-flight chat
call has stalled or blown past the absolute ceiling.

**M11a-1 ships this module as an empty scaffold.** The
``RECOMMENDED_STALL_THRESHOLDS_BY_MODEL`` map is intentionally
empty; ``resolve_stall_thresholds(model)`` falls through to
``RECOMMENDED_DEFAULT_STALL`` for every input, which mirrors the
global defaults currently encoded in
``consultants/engine/control.py``. This preserves the M12 parity
guarantee: importing this module changes **no** runtime behavior.

The M11a-2 commit (the live skill-eval run + closeout) populates
the per-model map from measured data and bumps the provenance
stamps below. When that happens, the engine wires
``resolve_stall_thresholds()`` into ``RuntimeControl`` defaults so
a researcher / coder / tool_executor lane that targets a known
model gets a tuned threshold instead of the global guess.

Mirror pattern: this module is the stall-side sibling of
``consultants/engine/coder_defaults.py``. Same provenance fields,
same "live config wins" rule (a TOML override in
``[runtime]`` always supersedes these constants).

**Fallback for out-of-cohort models**: ``resolve_stall_thresholds``
returns :data:`RECOMMENDED_DEFAULT_STALL` (matching the existing
global ``control.DEFAULT_STALL_THRESHOLD_S=300`` /
``PER_LANE_HARD_S_DEFAULT=3600``) for any model NOT in
:data:`RECOMMENDED_STALL_THRESHOLDS_BY_MODEL`. The fallback is
deliberately conservative — 5 of the 7 measured cohort models
want ``stall_threshold_s <= 150``, so ``300`` is wider than most
cohort cases. If a new model trips on the fallback, the operator
has two cheap escape valves:

1. **Add the model to the M11a cohort and re-run the bench** —
   ``claude-consultants skill-eval stall --live --tier1
   --models <existing,new-model:v0> --accept-cost``.
2. **TOML override** — ``[runtime].stall_threshold_s = ...`` in
   ``consultants.toml`` always wins over these constants per the
   "live config wins" rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ====================================================================== #
# Provenance
# ====================================================================== #

# The date of the most-recent skill-eval run that informed these
# defaults. M11a-2 closed with a Tier-1 live run on 2026-05-17
# (84 trials, 7 cohort models, zero errors).
RECOMMENDED_AS_OF: str = "2026-05-17"

# Suite version of the stall skill-eval the recommendation maps
# to. The suite manifest lives at
# ``benchmarks/consultants/questions/stall/SUITE.md``.
RECOMMENDED_SUITE_VERSION: str = "1.0"

# First 8 chars of the suite manifest hash this rec was scored
# against. Set by M11a-2 from the live run's
# ``metadata.json:suite_hash``. If a baselines row claims this
# suite version but the hash doesn't match, the question content
# drifted without a version bump — investigate before trusting
# the score.
RECOMMENDED_SUITE_HASH_PREFIX: str = "c8306c62"

# Tier mix the recommendations were derived from. Tier 1
# (standalone) is the Tier-1-only M11a-2 close. A future Tier 2
# (council) re-run would bump this string and update the
# per-model entries below; the derivation rule prefers Tier 2
# numbers when present.
RECOMMENDED_TIER_MIX: str = "tier1-only"


# ====================================================================== #
# Data shape
# ====================================================================== #

@dataclass(frozen=True)
class StallThresholds:
    """Per-model stall + hard-cap recommendation.

    ``stall_threshold_s`` — how long the watchdog waits without
    seeing a new token before declaring a stall (startup or
    mid-stream). The derivation rule M11a-2 will apply::

        stall_threshold_s = max(p99_inter_token, p99_ttft) * 2.5,
                            in seconds, rounded up to the nearest
                            30s, floored at 30s, ceiled at 600s.

    ``hard_cap_s`` — the absolute wall-clock ceiling per lane.
    Beyond this the lane is tombstoned regardless of stall state::

        hard_cap_s        = p99(wall_s) * 3.0, rounded up to the
                            nearest 60s, floored at 300s,
                            ceiled at 3600s.

    Both are seconds. Match the units on
    ``consultants/engine/control.py:DEFAULT_STALL_THRESHOLD_S`` and
    ``PER_LANE_HARD_S_DEFAULT``.
    """

    stall_threshold_s: float
    hard_cap_s: float


# ====================================================================== #
# Defaults (M11a-1 scaffold; populated by M11a-2)
# ====================================================================== #

# Global default — used when the requested model isn't in the
# per-model map below. Matches ``control.DEFAULT_STALL_THRESHOLD_S``
# (300 s) and ``control.PER_LANE_HARD_S_DEFAULT`` (3600 s) exactly,
# so swapping ``resolve_stall_thresholds()`` into the runtime path
# in M11a-2 is a no-op for un-measured models.
RECOMMENDED_DEFAULT_STALL: StallThresholds = StallThresholds(
    stall_threshold_s=300.0,
    hard_cap_s=3600.0,
)

# Per-model overrides. Populated from the M11a-2 Tier-1 live run
# at ``benchmarks/consultants/results/2026-05-17/stall-tier1/``
# (suite v1.0, hash c8306c62; 84 trials × 7 cohort models × 4
# standalone questions × 3 trials each; zero errors; 61 min total
# wall).
#
# Derivation rule (from
# ``benchmarks/consultants/stall_bench.py:derive_thresholds``):
#
#   stall_threshold_s = max(p99_inter_token_ms, p99_ttft_ms) * 2.5,
#                       in seconds, rounded up to the nearest 30 s,
#                       floored at 30 s, ceiled at 600 s.
#   hard_cap_s        = p99(wall_s) * 3.0,
#                       rounded up to the nearest 60 s,
#                       floored at 300 s, ceiled at 3600 s.
#
# Keys exactly match the model tags the agent loop uses (e.g.
# ``"glm-5.1:cloud"``, not ``"glm-5.1"``).
#
# **Notable findings**:
# - `kimi-k2.6:cloud` needs ``stall_threshold_s=390`` — higher
#   than the global ``DEFAULT_STALL_THRESHOLD_S=300``. The current
#   global default would have falsely tripped ``STARTUP_STALL``
#   on kimi calls (p99 TTFT measured at 150 s, derived stall
#   threshold 150 * 2.5 = 375 s → rounded up to 390).
# - `deepseek-v4-flash:cloud` needs ``hard_cap_s=780`` — its p99
#   wall hit 256 s on this 4-question batch; 256 * 3 = 768 →
#   rounded up to 780.
# - Three fast models (qwen3-coder-next, gemma4:31b,
#   gemini-3-flash-preview) clamp at the ``stall_min_s=30``
#   floor — they'd genuinely tolerate sub-30 s thresholds, but
#   the floor protects against hair-trigger detection on
#   tiny inter-token noise.
RECOMMENDED_STALL_THRESHOLDS_BY_MODEL: dict[str, StallThresholds] = {
    "glm-5.1:cloud": StallThresholds(
        stall_threshold_s=90.0, hard_cap_s=300.0,
    ),
    # Inherited from glm-5.1, NOT re-measured. The 5.1 row above is a
    # real M11a measurement; this one is a succession default so 5.2
    # gets sane thresholds instead of falling through to the generic
    # floor. Re-run the stall bench to earn a measured row.
    "glm-5.2:cloud": StallThresholds(
        stall_threshold_s=90.0, hard_cap_s=300.0,
    ),
    "kimi-k2.6:cloud": StallThresholds(
        stall_threshold_s=390.0, hard_cap_s=540.0,
    ),
    "gemma4:31b-cloud": StallThresholds(
        stall_threshold_s=30.0, hard_cap_s=300.0,
    ),
    "qwen3-coder-next:cloud": StallThresholds(
        stall_threshold_s=30.0, hard_cap_s=300.0,
    ),
    "deepseek-v4-pro:cloud": StallThresholds(
        stall_threshold_s=150.0, hard_cap_s=300.0,
    ),
    "deepseek-v4-flash:cloud": StallThresholds(
        stall_threshold_s=210.0, hard_cap_s=780.0,
    ),
    "gemini-3-flash-preview:cloud": StallThresholds(
        stall_threshold_s=30.0, hard_cap_s=300.0,
    ),
}


# ====================================================================== #
# Resolver
# ====================================================================== #

def resolve_stall_thresholds(model: Optional[str]) -> StallThresholds:
    """Return the recommended ``(stall_threshold_s, hard_cap_s)`` for
    ``model``, or the global default if ``model`` has no per-model
    entry.

    ``model`` may be ``None`` (e.g. when the caller hasn't pinned a
    model yet); the global default is returned.

    Pure function; no I/O. Safe to call from any path.

    M11a-1: every model falls through to
    :data:`RECOMMENDED_DEFAULT_STALL`. M11a-2: cohort models that
    were scored get a tuned override; everything else still falls
    through.
    """
    if not model:
        return RECOMMENDED_DEFAULT_STALL
    return RECOMMENDED_STALL_THRESHOLDS_BY_MODEL.get(
        model, RECOMMENDED_DEFAULT_STALL,
    )


__all__ = [
    "StallThresholds",
    "RECOMMENDED_AS_OF",
    "RECOMMENDED_SUITE_VERSION",
    "RECOMMENDED_SUITE_HASH_PREFIX",
    "RECOMMENDED_TIER_MIX",
    "RECOMMENDED_DEFAULT_STALL",
    "RECOMMENDED_STALL_THRESHOLDS_BY_MODEL",
    "resolve_stall_thresholds",
]
