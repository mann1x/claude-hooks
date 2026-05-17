"""Defaults for the optional tool_executor role (M6), grounded
in the M11c skill-eval bench.

**M11c-1 ships this module as an empty scaffold.** The
``RECOMMENDED_TOOL_EXECUTOR_MODEL`` is a sentinel string (empty);
``RECOMMENDED_DEFAULT_ON`` is ``False``, matching the current
``DEFAULT_ENABLED_BY_ROLE["tool_executor"]=False`` in
``consultants/config.py`` exactly. This preserves the M12 parity
guarantee: importing this module changes **no** runtime behavior.

The M11c-2 commit (the live skill-eval run + closeout) populates
``RECOMMENDED_TOOL_EXECUTOR_MODEL`` from measured data and bumps
the provenance stamps below. Whether ``RECOMMENDED_DEFAULT_ON``
flips from ``False`` to ``True`` is gated by **both**:

1. The bench winner passing the rubric (``pass_rate >= 70%``
   AND ``avg_quality >= 3.5``).
2. The x-tier composition decision (task #103) being resolved —
   EITHER the engine refactor (Option 2: per-lane
   ``awaiting_tool_results``) lands AND the role is safe at
   x-tier, OR the role is explicitly documented as base-tier-only
   (Option 1: doc deferral).

When that two-part gate fires, ``RECOMMENDED_DEFAULT_ON`` flips
True and a separate engine commit wires it into
``DEFAULT_ENABLED_BY_ROLE`` so a default-config consultation
gets the tool_executor role active. Until then this module is
inert and the role remains opt-in.

Mirror pattern: this module is the tool_executor-side sibling of
``consultants/engine/coder_defaults.py`` and
``consultants/engine/stall_defaults.py``. Same provenance fields,
same "live config wins" rule (a TOML override in
``[role.tool_executor]`` always supersedes these constants).

**Fallback for unknown configurations**: when
``RECOMMENDED_DEFAULT_ON=False`` (M11c-1 state), no engine wiring
fires from this module. The config layer reads
``DEFAULT_ENABLED_BY_ROLE`` and ``DEFAULT_MODEL_BY_ROLE``
unchanged from M6.
"""

from __future__ import annotations


# ====================================================================== #
# Provenance
# ====================================================================== #

# The date of the most-recent skill-eval run that informed these
# defaults. M11c-2 bumped this from the M11c-1 scaffold-landing
# date to the live-run date.
RECOMMENDED_AS_OF: str = "2026-05-17"

# Suite version of the tool_executor skill-eval the recommendation
# maps to. The suite manifest lives at
# ``benchmarks/consultants/questions/tool_executor/SUITE.md``.
RECOMMENDED_SUITE_VERSION: str = "1.0"

# First 8 chars of the suite manifest hash this rec was scored
# against. If a baselines row claims this suite version but the
# hash doesn't match, the question content drifted without a
# version bump — investigate before trusting the score.
RECOMMENDED_SUITE_HASH_PREFIX: str = "7921555c"


# ====================================================================== #
# Defaults (M11c-2 closeout, 2026-05-17)
# ====================================================================== #

# The recommended model for ``cfg.roles.tool_executor.model``
# when the role is enabled.
#
# **M11c-2 winner**: ``gemma4:31b-cloud``. The M11c-2 live bench
# (48 trials across the 6-model cohort × 8 questions × 1 trial)
# produced four models tied on pass rate at 87.5% (glm-5.1,
# kimi-k2.6, gemma4:31b, deepseek-v4-pro) and one at 75%
# (gemini-3-flash-preview); qwen3-coder-next failed the rubric
# at 62.5%. Among the qualifying tie, ``gemma4:31b-cloud`` won
# every tiebreaker:
#
#   - **Perfect avg judge quality**: 5.00 / 5.00 (vs 4.12-4.50
#     for the other tied models).
#   - **Fastest avg wall**: 4.9 s per trial (vs 6.7-11.3 s for
#     the other tied models).
#   - **Low tool-call cost**: 2.6 calls per trial on average,
#     beaten only by glm-5.1 (2.2) but glm-5.1's quality drag
#     (4.12) cost it the tiebreaker.
#
# Notably this matches the M6 fallback default
# (``DEFAULT_MODEL_BY_ROLE["tool_executor"]="gemma4:31b-cloud"``
# from ``consultants/config.py``) — the empirical bench
# confirmed the trace-data intuition. Baseline row in
# ``docs/consultants-skill-eval-baselines.md``.
RECOMMENDED_TOOL_EXECUTOR_MODEL: str = "gemma4:31b-cloud"

# Whether the role should be enabled by default.
#
# **M11c-2 decision**: stays ``False``. The bench winner cleared
# part 1 of the gate (rubric pass: 87.5% / 5.00 — well above the
# 70% / 3.5 floors). Part 2 (task #103 — x-tier proper
# composition) is NOT yet resolved, so per the M11c plan
# (``/root/.claude/plans/recursive-petting-planet.md``) the
# default-on bit stays disabled until #103 lands:
#
# 1. ✅ Bench winner clears the rubric (``pass_rate=87.5%`` AND
#    ``avg_quality=5.00``).
# 2. ❌ Task #103 (x-tier proper composition) — NOT resolved.
#    EITHER the engine refactor lands (Option 2) so the role
#    composes correctly under multi-model researcher fanout, OR
#    the role is explicitly documented as base-tier-only
#    (Option 1 doc deferral) and runtime gates apply.
#
# When part 2 resolves, a separate engine commit reads this
# constant and wires it into the runtime's
# ``DEFAULT_ENABLED_BY_ROLE`` lookup. Until then this stays
# False — preserving the [[feedback_xtier_diversity_priority]]
# constraint that Phase 9 multi-model researcher fanout is the
# council's defining advantage and must NOT be auto-gated off.
RECOMMENDED_DEFAULT_ON: bool = False


__all__ = [
    "RECOMMENDED_AS_OF",
    "RECOMMENDED_SUITE_VERSION",
    "RECOMMENDED_SUITE_HASH_PREFIX",
    "RECOMMENDED_TOOL_EXECUTOR_MODEL",
    "RECOMMENDED_DEFAULT_ON",
]
