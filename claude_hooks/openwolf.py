"""
OpenWolf integration — extract learnings from .wolf/ files for cross-project memory.

When a project uses OpenWolf (https://github.com/cytostack/openwolf), its
``.wolf/`` directory contains per-project learning data:

- ``cerebrum.md``  — user preferences, key learnings, do-not-repeat mistakes,
  decision log
- ``buglog.json``  — auto-detected and manually logged bugs with root causes

claude-hooks reads these at one point:

**Recall** (UserPromptSubmit) — inject the Do-Not-Repeat section + recent
bugs so the model never forgets past mistakes in this project.

(There used to be a "Store" path that appended cerebrum + buglog content
to every Qdrant entry on Stop. Removed 2026-04-27 — it duplicated ~3KB of
boilerplate into every stored turn, dominated cosine similarity, and added
no recall value because the same data is already injected fresh on every
UserPromptSubmit.)
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.openwolf")


def wolf_dir(cwd: str) -> Optional[Path]:
    """Return the .wolf/ directory if it exists in the project, else None."""
    if not cwd:
        return None
    p = Path(cwd) / ".wolf"
    return p if p.is_dir() else None


# ---------------------------------------------------------------------- #
# Recall: extract context for injection
# ---------------------------------------------------------------------- #
def recall_context(cwd: str) -> Optional[str]:
    """
    Build a markdown block from OpenWolf data worth injecting into the prompt.
    Returns None if no .wolf/ or nothing useful.
    """
    wd = wolf_dir(cwd)
    if not wd:
        return None

    parts: list[str] = []

    # Do-Not-Repeat from cerebrum.md
    dnr = _extract_section(wd / "cerebrum.md", "Do-Not-Repeat")
    if dnr:
        parts.append(f"**Do-Not-Repeat (this project)**\n{dnr}")

    # Recent bugs from buglog.json (last 5)
    bugs = _recent_bugs(wd / "buglog.json", limit=5)
    if bugs:
        lines = [f"- **{b['id']}** {b.get('file','?')}: {b.get('error_message','?')} → {b.get('fix','?')}" for b in bugs]
        parts.append(f"**Recent bugs (this project)**\n" + "\n".join(lines))

    if not parts:
        return None
    return "### OpenWolf\n" + "\n\n".join(parts)


def _extract_section(path: Path, heading: str) -> Optional[str]:
    """Extract content under a ## heading from a markdown file."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    pattern = rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)"
    m = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    if not m:
        return None

    content = m.group(1).strip()
    # Strip HTML comments
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL).strip()
    if not content or len(content) < 10:
        return None
    return content


def _recent_bugs(path: Path, limit: int = 5) -> list[dict]:
    """Return the most recent bugs from buglog.json."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    bugs = data.get("bugs") or []
    if not bugs:
        return []
    # Sort by last_seen descending, take latest
    bugs.sort(key=lambda b: b.get("last_seen", ""), reverse=True)
    return bugs[:limit]


