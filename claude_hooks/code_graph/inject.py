"""
Read GRAPH_REPORT.md and produce a SessionStart additionalContext block.

Hard-capped to ``max_chars`` so we never blow the prompt budget. We
truncate at a paragraph boundary when possible — a half-cut bullet
list is uglier than a clean cut at "## How to use".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from claude_hooks.code_graph.detect import graph_report_path

log = logging.getLogger("claude_hooks.code_graph.inject")

# A graphify-style report at top_n=10 lands around 1.5–2.5 kB on a
# medium repo, well under this cap. The cap exists for monorepos.
DEFAULT_MAX_CHARS = 4000


def build_session_block(
    root: Path,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Optional[str]:
    """Return the markdown block to inject, or None if there's nothing yet.

    Wraps the report in a small header so the model knows where it
    came from and that it's a *summary*, not the source itself.
    """
    rp = graph_report_path(root)
    if not rp.exists():
        return None
    try:
        body = rp.read_text(encoding="utf-8")
    except OSError as e:
        log.debug("could not read %s: %s", rp, e)
        return None
    if not body.strip():
        return None

    body = _truncate(body, max_chars)
    return (
        "## Project code graph\n\n"
        f"_Pre-built structural summary (full graph: `graphify-out/graph.json`)._\n\n"
        f"{body}"
    )


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    # Try to cut at the last "## " heading boundary before the cap.
    cut = text.rfind("\n## ", 0, max_chars)
    if cut > max_chars // 2:
        return text[:cut].rstrip() + "\n\n_(report truncated — see full GRAPH_REPORT.md)_\n"
    return text[:max_chars].rstrip() + "\n\n_(report truncated)_\n"
