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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ====================================================================== #
# Provenance
# ====================================================================== #

# The date of the most-recent skill-eval run that informed these
# defaults. In M11a-1 the scaffold is empty so this points at the
# scaffold-landing date rather than a live-run date; M11a-2 bumps
# it to the live-run date.
RECOMMENDED_AS_OF: str = "2026-05-17 (scaffold — no live run yet)"

# Suite version of the stall skill-eval the recommendation maps
# to. The suite manifest lives at
# ``benchmarks/consultants/questions/stall/SUITE.md``.
RECOMMENDED_SUITE_VERSION: str = "1.0"

# First 8 chars of the suite manifest hash this rec was scored
# against. Empty in M11a-1 (no live data yet); M11a-2 fills it
# in. If a baselines row claims this suite version but the hash
# doesn't match, the question content drifted without a version
# bump — investigate before trusting the score.
RECOMMENDED_SUITE_HASH_PREFIX: str = ""


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

# Per-model overrides. **Empty in M11a-1.** M11a-2 populates this
# with one entry per cohort model from the live skill-eval run.
# Keep keys exactly matching the model tags the agent loop uses
# (e.g. "glm-5.1:cloud", not "glm-5.1").
RECOMMENDED_STALL_THRESHOLDS_BY_MODEL: dict[str, StallThresholds] = {}


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
    "RECOMMENDED_DEFAULT_STALL",
    "RECOMMENDED_STALL_THRESHOLDS_BY_MODEL",
    "resolve_stall_thresholds",
]
