"""Defaults for the now-enabled-by-default tool_executor role
(M6), grounded in the M11c skill-eval bench.

**M11c-5 (2026-05-17) flipped this role's default-on bit to
``True``.** The two-part gate from the M11c plan cleared:

1. ✅ The M11c-2 bench winner cleared the rubric:
   ``gemma4:31b-cloud`` at ``pass_rate=87.5%`` AND
   ``avg_quality=5.00`` (won every tiebreaker among 4 tied
   models). Baselines table in
   ``docs/consultants-skill-eval-baselines.md``.
2. ✅ Task #103 (x-tier proper composition) resolved via the
   M11c-3 engine refactor (commit ``e62fd85``): per-lane
   ``parent_lane_idx`` threading + the new
   ``_fanout_after_tool_executor`` conditional edge replacing
   the M6 unconditional ``tool_executor → researcher`` edge.
   The headline no-cross-pollution test
   (``test_two_researcher_lanes_three_items_each_no_pollution``)
   is green; ``tests/test_consultants_v2_tool_executor_xtier_composition.py``
   pins the contract for future regressions.

Mirror pattern: this module is the tool_executor-side sibling of
``consultants/engine/coder_defaults.py`` and
``consultants/engine/stall_defaults.py``. Same provenance fields,
same "live config wins" rule (a TOML override in
``[role.tool_executor]`` always supersedes these constants).

**Override-out semantics**: setting ``[role.tool_executor]
enabled = false`` in a config still disables the role (operators
needing the M6-era researcher-with-inline-tool-subloop topology
get that with one TOML line). The default change here only
affects fresh configs without an explicit
``[role.tool_executor].enabled`` declaration.
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
# **M11c-5 decision (2026-05-17): flipped to ``True``.** Both
# parts of the M11c plan's two-part gate cleared:
#
# Flip history:
# - M11c-1 (2026-05-17): scaffold False.
# - M11c-5 (2026-05-17): flipped True after the two-part gate
#   cleared — M11c-2 bench rubric pass (87.5% / 5.00) +
#   task #103 (x-tier proper composition) via M11c-3 engine
#   refactor (commit ``e62fd85``).
# - 2026-05-18: flipped back to False. The M14 first-real-ask
#   tool_executor on/off A/B
#   (``benchmarks/consultants/results/2026-05-18/tool-executor-ab/``)
#   showed the role costing +12 minutes wall time and +43% tokens
#   on a grep-shaped question AND identifying fewer edge cases
#   than the inline-tool-loop variant. The M11c-2 bench result
#   still stands for the tool-heavy question shape that bench
#   targeted; the default flip-back recognizes most operator
#   questions don't look like the bench corpus.
#
# This module remains the bench-grounded provenance record for
# the role. Setting ``RECOMMENDED_DEFAULT_ON = False`` here does
# NOT delete the M11c-2 win — it records the operator-side
# trade-off that informs the default. The
# ``RECOMMENDED_TOOL_EXECUTOR_MODEL`` constant still surfaces the
# bench-validated pick (``gemma4:31b-cloud``) for operators who
# opt in via TOML.
#
# Atomic with ``consultants/config.py:DEFAULT_ENABLED_BY_ROLE``
# — the M12 parity test
# ``test_scaffold_default_on_matches_runtime_default`` enforces
# bit-for-bit alignment.
RECOMMENDED_DEFAULT_ON: bool = False


__all__ = [
    "RECOMMENDED_AS_OF",
    "RECOMMENDED_SUITE_VERSION",
    "RECOMMENDED_SUITE_HASH_PREFIX",
    "RECOMMENDED_TOOL_EXECUTOR_MODEL",
    "RECOMMENDED_DEFAULT_ON",
]
