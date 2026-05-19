"""Validator — applies schema rules to a parsed AST.

The rules are pinned at module import time (no runtime config of
the validator itself); to relax a rule for a specific deployment,
override ``validate`` from a wrapping package.
"""

from __future__ import annotations


_REQUIRED_KEYS = {"name", "version"}


def validate(ast: list[tuple[str, str]]) -> None:
    """Raise ValueError when ``ast`` is missing a required key."""
    seen = {k for k, _ in ast}
    missing = _REQUIRED_KEYS - seen
    if missing:
        raise ValueError(f"missing required keys: {sorted(missing)}")
