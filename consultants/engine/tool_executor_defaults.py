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
# defaults. In M11c-1 the scaffold is empty so this points at the
# scaffold-landing date rather than a live-run date; M11c-2 bumps
# it to the live-run date.
RECOMMENDED_AS_OF: str = "2026-05-17 (scaffold — no live run yet)"

# Suite version of the tool_executor skill-eval the recommendation
# maps to. The suite manifest lives at
# ``benchmarks/consultants/questions/tool_executor/SUITE.md``.
RECOMMENDED_SUITE_VERSION: str = "1.0"

# First 8 chars of the suite manifest hash this rec was scored
# against. Empty in M11c-1 (no live data yet); M11c-2 fills it
# in. If a baselines row claims this suite version but the hash
# doesn't match, the question content drifted without a version
# bump — investigate before trusting the score.
RECOMMENDED_SUITE_HASH_PREFIX: str = ""


# ====================================================================== #
# Defaults (M11c-1 scaffold; populated by M11c-2)
# ====================================================================== #

# The recommended model for ``cfg.roles.tool_executor.model``
# when the role is enabled. Empty string in M11c-1 means "no
# recommendation yet" — the config layer falls through to the
# existing ``DEFAULT_MODEL_BY_ROLE["tool_executor"]=
# "gemma4:31b-cloud"`` from M6.
RECOMMENDED_TOOL_EXECUTOR_MODEL: str = ""

# Whether the role should be enabled by default. ``False`` in
# M11c-1 (matches ``DEFAULT_ENABLED_BY_ROLE["tool_executor"]
# =False``). M11c-2 may flip this to ``True`` IF:
#
# 1. The bench winner clears the rubric (``pass_rate >= 70%``
#    AND ``avg_quality >= 3.5``).
# 2. Task #103 (x-tier proper composition) is resolved — EITHER
#    the engine refactor lands (Option 2) so the role composes
#    correctly under multi-model researcher fanout, OR the role
#    is explicitly documented as base-tier-only (Option 1 doc
#    deferral) and runtime gates apply.
#
# When that two-part gate fires, a separate engine commit reads
# this constant and wires it into the runtime's
# ``DEFAULT_ENABLED_BY_ROLE`` lookup. Until then this stays False.
RECOMMENDED_DEFAULT_ON: bool = False


__all__ = [
    "RECOMMENDED_AS_OF",
    "RECOMMENDED_SUITE_VERSION",
    "RECOMMENDED_SUITE_HASH_PREFIX",
    "RECOMMENDED_TOOL_EXECUTOR_MODEL",
    "RECOMMENDED_DEFAULT_ON",
]
