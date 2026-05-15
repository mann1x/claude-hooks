"""Shared MCP output formatters for pgvector-mcp + sqlite-vec-mcp.

Both servers expose the same conceptual tool catalog (find, find-hybrid,
store, count, kg_search, kg_create, kg_observe, kg_relate). The
human-readable text payload for ``content[].text`` should look the same
across both — keeps downstream parsers (Codex, Cursor, OpenWebUI) from
having to special-case backend.

Two formatters live here:

* :func:`format_memories` — vector / hybrid recall results.
* :func:`format_kg_nodes`  — knowledge-graph search results.

Lifted verbatim from ``pgvector_mcp/server.py``; both servers now import
from this module so the rendered output is byte-identical for the same
input.
"""

from __future__ import annotations

from typing import Iterable


def format_memories(mems: Iterable) -> str:
    """Render a list of ``Memory`` objects as a turn-delimited block."""
    out = []
    for m in mems:
        meta = getattr(m, "metadata", None) or {}
        score = meta.get("_score")
        dist = meta.get("_distance")
        tbl = meta.get("_table") or "?"
        head = f"[{tbl}"
        if score is not None:
            head += f" score={score:.4f}"
        if dist is not None:
            head += f" dist={dist:.4f}"
        head += "]"
        out.append(f"{head} {getattr(m, 'text', '')}")
    if not out:
        return "(no results)"
    return "\n\n---\n\n".join(out)


def format_kg_nodes(nodes: list[dict]) -> str:
    """Render a list of KG nodes as a numbered ``# name (type)`` list."""
    if not nodes:
        return "(no results)"
    out = []
    for n in nodes:
        head = (
            f"# {n['name']} ({n['entity_type']})  "
            f"score={n.get('_score', 0):.3f} match={n.get('_match', '?')}"
        )
        body = "\n".join(f"  - {o}" for o in n.get("observations", []))
        if not body:
            body = "  (no observations)"
        out.append(f"{head}\n{body}")
    return "\n\n".join(out)
