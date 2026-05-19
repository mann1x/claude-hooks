"""Defaults for the optional coder role (M10), grounded in the
2026-05-16 M11b skill-eval run (suite v1.0, single-language) and
the 2026-05-17 M11b-mlang v1.0.1 multi-language delta.

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
# the 2026-05-16 M11b winner). New per-language routing (task #111)
# is anchored on the v1.0.1-mlang delta and lives below.
RECOMMENDED_CODER_MODEL: str = "glm-5.1:cloud"

# The date of the most-recent run that informed these defaults.
# Stamp stays even if the constants don't change — proves the
# recommendation is current.
RECOMMENDED_AS_OF: str = "2026-05-17"

# Suite version the recommendation was scored against. The mlang
# v1.0.1 delta supersedes v1.0 for per-language routing; v1.0 still
# anchors the single-model fallback above.
RECOMMENDED_SUITE_VERSION: str = "1.0.1-mlang"

# First 8 chars of the suite manifest hash this rec was scored
# against. If a baseline row claims this suite version but the
# hash doesn't match, the question content drifted without a
# version bump — investigate before trusting the score.
RECOMMENDED_SUITE_HASH_PREFIX: str = "ddef8095"

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


# Per-language route table — the v1.0.1-mlang winners with the
# user's explicit overrides (2026-05-17 task #111). ``primary`` is
# the alg/quality winner per language; ``fallback`` is the
# second-best avgQ model from the same cohort, so a failover lands
# on a still-strong-for-that-language candidate rather than a
# random survivor.
#
# Override basis:
# - csharp.primary: user override (table avgQ winner; tied for alg)
# - python.primary: user override (table avgQ winner; tied)
# - c.primary:      user "pick fastest" (table avgQ winner is pro
#                   but the alg axis is a 3-way tie at 50%, so the
#                   user chose glm for cohort consistency + speed)
# - cpp.primary:    table alg+quality winner (flash; only model
#                   with any cpp alg-pass)
# - go.primary:     table avgQ winner (kimi; cohort 0% alg, so
#                   quality is the only discriminator)
# - rust.primary:   table avgQ winner (flash)
#
# Fallback basis: cohort-wide rank #2 for that language's avgQ
# when not already the primary; pro fills the gap as the
# cross-language #2 in most rows.
RECOMMENDED_CODER_ROUTES_BY_LANGUAGE: dict[str, CoderLanguageRoute] = {
    "c":      CoderLanguageRoute(primary="glm-5.1:cloud",
                                  fallback="deepseek-v4-pro:cloud"),
    "cpp":    CoderLanguageRoute(primary="deepseek-v4-flash:cloud",
                                  fallback="kimi-k2.6:cloud"),
    "csharp": CoderLanguageRoute(primary="deepseek-v4-pro:cloud",
                                  fallback="kimi-k2.6:cloud"),
    "go":     CoderLanguageRoute(primary="kimi-k2.6:cloud",
                                  fallback="deepseek-v4-pro:cloud"),
    "python": CoderLanguageRoute(primary="glm-5.1:cloud",
                                  fallback="kimi-k2.6:cloud"),
    "rust":   CoderLanguageRoute(primary="deepseek-v4-flash:cloud",
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
    primary="glm-5.1:cloud",
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
    "QUALIFYING_MODELS_2026_05_16",
    "QUALIFYING_MODELS_2026_05_17_MLANG",
    "RECOMMENDED_AS_OF",
    "RECOMMENDED_CODER_DEFAULT_ROUTE",
    "RECOMMENDED_CODER_MODEL",
    "RECOMMENDED_CODER_ROUTES_BY_LANGUAGE",
    "RECOMMENDED_SUITE_HASH_PREFIX",
    "RECOMMENDED_SUITE_VERSION",
    "language_from_path",
    "resolve_coder_route",
]
