"""Re-export of :mod:`claude_hooks.staleness`.

The detector moved up when the ``lsp_engine`` daemon needed it too: the
engine layer must not import from the MCP layer above it. This keeps the
old import path working.
"""

from claude_hooks.staleness import (  # noqa: F401
    DEFAULT_MIN_RECHECK_SECONDS,
    DETECTOR,
    ENV_DISABLE,
    IMPORT_TIME,
    Report,
    StalenessDetector,
    banner,
)

__all__ = [
    "DEFAULT_MIN_RECHECK_SECONDS", "DETECTOR", "ENV_DISABLE", "IMPORT_TIME",
    "Report", "StalenessDetector", "banner",
]
