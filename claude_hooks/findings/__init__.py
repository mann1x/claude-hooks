"""Council findings: a per-project ledger of claims about the code and
what was decided about them (docs/PLAN-council-memory.md).

The files under ``<project>/.claude-hooks/findings/`` are the record. A
council records candidates; only the user or the assistant settles them.
"""
from __future__ import annotations

from typing import Optional

from claude_hooks.findings.ledger import FindingNotFound, Ledger, findings_root
from claude_hooks.findings.model import (
    BY_DESIGN, CANDIDATE, CONFIRMED, FIXED, REFUTED, SETTLED, STATUSES,
    Finding,
)


def findings_enabled(config: Optional[dict] = None) -> bool:
    """On unless ``hooks.findings.enabled`` is false."""
    if config is None:
        try:
            from claude_hooks.config import load_config
            config = load_config()
        except Exception:
            return True
    section = ((config or {}).get("hooks") or {}).get("findings") or {}
    return bool(section.get("enabled", True))


__all__ = ["Finding", "Ledger", "FindingNotFound", "findings_root",
           "findings_enabled", "STATUSES", "SETTLED", "CANDIDATE",
           "CONFIRMED", "REFUTED", "BY_DESIGN", "FIXED"]
