"""Serializer — turns a parsed AST back into bytes for transport.

The default encoding is UTF-8 with deterministic key ordering so
two runs over the same AST produce byte-identical output.
"""

from __future__ import annotations


def dump(ast: list[tuple[str, str]]) -> bytes:
    """Serialize ``ast`` to bytes."""
    parts = []
    for k, v in sorted(ast):
        parts.append(f"{k}={v}")
    return ("\n".join(parts) + "\n").encode("utf-8")
