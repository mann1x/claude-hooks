"""Defaults for the optional coder role (M10), grounded in the
2026-05-16 M11b skill-eval run (suite v1.0, single-language), the
2026-05-17 M11b-mlang v1.0.1 multi-language delta, and the
2026-06-04 ``coder_med`` v1.0 cross-judge run (which set the
current per-language routing to the neutral-gemini-ladder winners).

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

from __future__ import annotations

import os
from typing import Optional

# Local import — the route dataclass lives in state_v2 so the
# engine + tests share a single shape.
from consultants.engine.state_v2 import CoderLanguageRoute


# ====================================================================== #
# Recommended defaults (2026-05-16 M11b run, suite v1.0;
#                       2026-05-17 M11b-mlang delta, suite v1.0.1)
# ====================================================================== #

# Legacy single-model recommendation (kept as the fallback when the
# per-language map has no entry AND no global default is set; matches
# the 2026-05-16 M11b winner, re-corroborated as the balanced
# token/wall-vs-quality efficiency winner by the 2026-06-04 coder_med
# run). New per-language routing (task #111) is anchored on the
# coder_med v1.0 neutral-ladder winners and lives below.
#: Model successions: ``{superseded: successor}``.
#:
#: A benchmark result belongs to the exact model tag that ran. When a
#: vendor ships a point release the operator wants routed to, rewriting
#: the cohort lists below would claim the successor earned scores it
#: never ran for — so the cohorts stay frozen and the succession is
#: declared here instead. Routing may name a successor of a qualifying
#: model; the score is *inherited, not re-measured*, and a fresh bench
#: is what turns an inherited route into an earned one.
#:
#: 2026-08-01: glm-5.2 supersedes glm-5.1 (operator decision).
MODEL_SUCCESSIONS: dict[str, str] = {
    "glm-5.1:cloud": "glm-5.2:cloud",
}


def successor_of(model: str) -> str:
    """The tag that should actually be routed for ``model``."""
    return MODEL_SUCCESSIONS.get(model, model)


def qualifying_with_successors(models) -> set:
    """``models`` plus every declared successor — the set a route may
    legitimately name."""
    out = set(models)
    for m in models:
        out.add(successor_of(m))
    return out


RECOMMENDED_CODER_MODEL: str = "glm-5.2:cloud"

# The date of the most-recent run that informed these defaults.
# Stamp stays even if the constants don't change — proves the
# recommendation is current.
RECOMMENDED_AS_OF: str = "2026-06-04"

# Suite version the per-language routing was scored against. The
# coder_med v1.0 cross-judge run supersedes the v1.0.1-mlang delta
# for per-language routing; the glm-5.1 single-model fallback +
# global default route are corroborated by coder_med's efficiency
# result (glm-5.1 wins balanced token/wall-vs-quality).
RECOMMENDED_SUITE_VERSION: str = "1.0-med"

# First 8 chars of the suite manifest hash this rec was scored
# against. If a baseline row claims this suite version but the
# hash doesn't match, the question content drifted without a
# version bump — investigate before trusting the score.
RECOMMENDED_SUITE_HASH_PREFIX: str = "0e6ab0fd"

# Qualifying-models list for the v1.0 single-model winner.
QUALIFYING_MODELS_2026_05_16: tuple[str, ...] = (
    "glm-5.1:cloud",          # winner: q=4.88, 1841 tok median
    "kimi-k2.6:cloud",        # q=4.86, 1957 tok, slower
    "gemma4:31b-cloud",       # q=4.62, slowest tail
    "qwen3-coder-next:cloud", # q=4.50, fastest but verbose
)

# Qualifying-models list for the v1.0.1-mlang delta (the 5 cohort
# models that ran across all 13 questions × 6 languages). Pinned
# here so the per-language defaults below have a transparent
# provenance.
QUALIFYING_MODELS_2026_05_17_MLANG: tuple[str, ...] = (
    "deepseek-v4-flash:cloud",
    "deepseek-v4-pro:cloud",
    "glm-5.1:cloud",
    "kimi-k2.6:cloud",
    "minimax-m2.7:cloud",
)

# Qualifying-models list for the 2026-06-04 coder_med v1.0 cohort
# (the 7 models that ran across all 10 problems × 6 languages). The
# per-language routes below only reference models from this set —
# note it adds minimax-m3 + nemotron-3-super over the mlang cohort.
QUALIFYING_MODELS_2026_06_04_MED: tuple[str, ...] = (
    "deepseek-v4-flash:cloud",
    "deepseek-v4-pro:cloud",
    "glm-5.1:cloud",
    "kimi-k2.6:cloud",
    "minimax-m2.7:cloud",
    "minimax-m3:cloud",
    "nemotron-3-super:cloud",
)


# ====================================================================== #
# Task #111: per-language routing
# ====================================================================== #

# File-extension → language-id map. The coder dispatcher reads
# ``CoderTaskItem.path``'s extension and looks up the language id
# here; an unknown / missing extension yields ``None`` and the
# global default route fires. Keep keys lower-case + the leading
# dot. New entries only need to be added when a new language enters
# the coder-suite cohort; out-of-cohort entries are fine — they
# resolve to the language id but fall through to the global default
# route at routing time (because no per-language entry exists).
LANGUAGE_BY_EXTENSION: dict[str, str] = {
    # In-cohort (v1.0.1-mlang)
    ".c":     "c",
    ".h":     "c",
    ".cpp":   "cpp",
    ".cxx":   "cpp",
    ".cc":    "cpp",
    ".hpp":   "cpp",
    ".hh":    "cpp",
    ".cs":    "csharp",
    ".go":    "go",
    ".py":    "python",
    ".pyi":   "python",
    ".rs":    "rust",
    # Out-of-cohort (resolve to a language id but no preset route)
    ".ts":    "typescript",
    ".tsx":   "typescript",
    ".js":    "javascript",
    ".jsx":   "javascript",
    ".mjs":   "javascript",
    ".java":  "java",
    ".kt":    "kotlin",
    ".kts":   "kotlin",
    ".swift": "swift",
    ".rb":    "ruby",
    ".php":   "php",
    ".sh":    "shell",
    ".bash":  "shell",
    ".zsh":   "shell",
}


def language_from_path(path: str) -> Optional[str]:
    """Map a sandbox-relative path to a language id via its
    extension. Empty path / no extension / unknown extension all
    return ``None``; the resolver then uses the global default
    route. Case-insensitive on the extension.
    """
    if not path:
        return None
    _, ext = os.path.splitext(path.strip())
    if not ext:
        return None
    return LANGUAGE_BY_EXTENSION.get(ext.lower())


# Per-language route table — the 2026-06-04 ``coder_med`` v1.0
# neutral-gemini-ladder winners (head-to-head ranking, model names
# anonymized + shuffled per question to kill name bias). ``primary``
# is the per-language ladder winner; ``fallback`` is the ladder
# runner-up, so a failover lands on the next-strongest model for that
# language rather than a random survivor.
#
# This is a QUALITY-FIRST table: ``kimi-k2.6`` is the primary for 5/6
# languages (it won the neutral ladder everywhere except cpp). It is
# also the most token-heavy of the cohort (~3.7× glm-5.1's completion
# tokens). Operators who want the balanced token/wall-vs-quality pick
# should override with ``[role.coder].model = "glm-5.2:cloud"`` (the
# efficiency winner); see ``docs/benchmarks/coder-med-results.md``.
#
# Ladder basis (primary = winner / fallback = runner-up, mean rank):
# - c:      kimi 2.10            / deepseek-v4-pro 3.25
# - cpp:    deepseek-v4-pro 1.95 / deepseek-v4-flash 3.45
# - csharp: kimi 2.40           / minimax-m3 2.75
# - go:     kimi 2.44           / deepseek-v4-pro 2.89
# - python: kimi 2.89           / deepseek-v4-flash 3.06
# - rust:   kimi 2.60           / deepseek-v4-pro 2.75
RECOMMENDED_CODER_ROUTES_BY_LANGUAGE: dict[str, CoderLanguageRoute] = {
    "c":      CoderLanguageRoute(primary="kimi-k2.6:cloud",
                                  fallback="deepseek-v4-pro:cloud"),
    "cpp":    CoderLanguageRoute(primary="deepseek-v4-pro:cloud",
                                  fallback="deepseek-v4-flash:cloud"),
    "csharp": CoderLanguageRoute(primary="kimi-k2.6:cloud",
                                  fallback="minimax-m3:cloud"),
    "go":     CoderLanguageRoute(primary="kimi-k2.6:cloud",
                                  fallback="deepseek-v4-pro:cloud"),
    "python": CoderLanguageRoute(primary="kimi-k2.6:cloud",
                                  fallback="deepseek-v4-flash:cloud"),
    "rust":   CoderLanguageRoute(primary="kimi-k2.6:cloud",
                                  fallback="deepseek-v4-pro:cloud"),
}

# Global default route — fires when the language id is None (no
# extension, unknown extension) OR when the language has no per-
# language entry. Pinned to the v1.0 single-model winner as
# primary (glm) with the v1.0.1-mlang cross-cohort runner-up
# (kimi) as failover. The fallback is deliberately a different
# vendor than the primary so a vendor-specific cloud incident
# doesn't take down both legs.
RECOMMENDED_CODER_DEFAULT_ROUTE: CoderLanguageRoute = CoderLanguageRoute(
    # glm-5.2 by succession from the glm-5.1 cohort winner — see
    # MODEL_SUCCESSIONS. Score inherited, not re-measured.
    primary="glm-5.2:cloud",
    fallback="kimi-k2.6:cloud",
)


def resolve_coder_route(
    language: Optional[str],
    *,
    routes_by_language: dict[str, CoderLanguageRoute],
    default_route: Optional[CoderLanguageRoute],
    legacy_model_fallback: str = "",
) -> CoderLanguageRoute:
    """Pure-function resolver — single source for the graph + tests.

    Resolution order:
    1. ``language`` in ``routes_by_language`` → that entry.
    2. ``default_route`` set → that entry.
    3. ``legacy_model_fallback`` non-empty → synthesize a
       ``CoderLanguageRoute(primary=legacy_model_fallback)`` so v1
       callers that never set ``routes_by_language`` or
       ``default_route`` still get a working chain.
    4. Otherwise raise ``ValueError`` — the caller passed nothing.

    The returned route's empty-fallback semantics ("no failover")
    are preserved verbatim; the lane code decides whether to fail
    fast or just call primary.
    """
    if language and language in routes_by_language:
        return routes_by_language[language]
    if default_route is not None:
        return default_route
    if legacy_model_fallback:
        return CoderLanguageRoute(primary=legacy_model_fallback)
    raise ValueError(
        "resolve_coder_route called with no routes, no default, "
        "and no legacy fallback — at least one must be provided"
    )


__all__ = [
    "LANGUAGE_BY_EXTENSION",
    "MODEL_SUCCESSIONS",
    "successor_of",
    "qualifying_with_successors",
    "QUALIFYING_MODELS_2026_05_16",
    "QUALIFYING_MODELS_2026_05_17_MLANG",
    "QUALIFYING_MODELS_2026_06_04_MED",
    "RECOMMENDED_AS_OF",
    "RECOMMENDED_CODER_DEFAULT_ROUTE",
    "RECOMMENDED_CODER_MODEL",
    "RECOMMENDED_CODER_ROUTES_BY_LANGUAGE",
    "RECOMMENDED_SUITE_HASH_PREFIX",
    "RECOMMENDED_SUITE_VERSION",
    "language_from_path",
    "resolve_coder_route",
]
