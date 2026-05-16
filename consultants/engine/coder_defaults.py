"""Defaults for the optional coder role (M10), grounded in the
2026-05-16 M11b skill-eval run.

The values here are the **recommended** defaults — they're loaded
by `consultants/config.py` only when a config TOML doesn't override
them. Live-config wins; a user who wants a different coder model
can set `[role.coder].model = "..."` and the value below never
runs. Don't hard-code references to this module anywhere except
the config factory.

When a new skill-eval run produces a different rubric winner:

1. Append a baselines row to
   ``docs/consultants-skill-eval-baselines.md``.
2. Update the constants in this file (and bump the date stamp).
3. Commit the two changes together so the source of the default
   and the evidence for it land in the same revision.
"""

# ====================================================================== #
# Recommended defaults (2026-05-16 M11b run, suite v1.0)
# ====================================================================== #

# The model that wins the v1.0 rubric. See
# ``docs/consultants-skill-eval-baselines.md`` for the table.
RECOMMENDED_CODER_MODEL: str = "glm-5.1:cloud"

# The date of the run that produced this recommendation. Stamp
# stays even if the constant doesn't change — proves the
# recommendation is current.
RECOMMENDED_AS_OF: str = "2026-05-16"

# Suite version the recommendation was scored against. A future
# suite version bump (e.g. 2.0 multi-language) requires
# re-baselining every prior model before this constant can be
# updated against the new suite.
RECOMMENDED_SUITE_VERSION: str = "1.0"

# First 8 chars of the suite manifest hash this rec was scored
# against. If a baseline row claims this suite version but the
# hash doesn't match, the question content drifted without a
# version bump — investigate before trusting the score.
RECOMMENDED_SUITE_HASH_PREFIX: str = "9aa6eaf0"

# Qualifying-models list — recorded for transparency so a future
# reader can see "the winner won among these candidates", not
# "this was the only model tried". Keeps blast radius visible if
# the winner is later disqualified.
QUALIFYING_MODELS_2026_05_16: tuple[str, ...] = (
    "glm-5.1:cloud",          # winner: q=4.88, 1841 tok median
    "kimi-k2.6:cloud",        # q=4.86, 1957 tok, slower
    "gemma4:31b-cloud",       # q=4.62, slowest tail
    "qwen3-coder-next:cloud", # q=4.50, fastest but verbose
)


__all__ = [
    "QUALIFYING_MODELS_2026_05_16",
    "RECOMMENDED_AS_OF",
    "RECOMMENDED_CODER_MODEL",
    "RECOMMENDED_SUITE_HASH_PREFIX",
    "RECOMMENDED_SUITE_VERSION",
]
