"""Shared MCP output formatters for pgvector-mcp + sqlite-vec-mcp.

Both servers expose the same conceptual tool catalog (find, find-hybrid,
store, count, kg_search, kg_create, kg_observe, kg_relate). The
human-readable text payload for ``content[].text`` should look the same
across both — keeps downstream parsers (Codex, Cursor, OpenWebUI) from
having to special-case backend.

Formatters live here:

* :func:`format_memories` — vector / hybrid recall results.
* :func:`format_kg_nodes`  — knowledge-graph search results.
* :func:`format_delete_result` — outcome of a deletion.

plus :func:`parse_hashes`, which both servers use to turn client-supplied
hex ids into ``bytes`` for ``delete_by_hashes``.

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
        # The row's content_hash, rendered so a caller can act on what it
        # just read. Deletion takes a hash, so omitting it here makes a
        # recalled memory readable but unaddressable — the client would
        # have to guess, or reproduce the text exactly and hope the
        # server's hash of it matches.
        chash = meta.get("_hash")
        if chash:
            head += f" id={chash}"
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


def parse_hashes(ids: Iterable) -> tuple[list[bytes], list[str]]:
    """Convert client-supplied hex ids to ``bytes`` for deletion.

    Returns ``(hashes, rejected)``. Anything that is not valid hex is
    rejected rather than coerced: a malformed id must be reported, never
    silently dropped, because "deleted 0" and "you gave me garbage" are
    different answers and only one of them tells the caller what to fix.

    Accepts an optional ``0x`` prefix and is case-insensitive, since the
    id is rendered lowercase but round-trips through models and shells.
    """
    hashes: list[bytes] = []
    rejected: list[str] = []
    seen: set[bytes] = set()
    for raw in ids or []:
        text = str(raw).strip()
        if text[:2].lower() == "0x":
            text = text[2:]
        if not text or len(text) % 2:
            rejected.append(str(raw))
            continue
        try:
            blob = bytes.fromhex(text)
        except ValueError:
            rejected.append(str(raw))
            continue
        if blob in seen:
            continue
        seen.add(blob)
        hashes.append(blob)
    return hashes, rejected


def format_delete_result(deleted: int, requested: int,
                         rejected: list[str]) -> str:
    """Render a deletion outcome, naming every id that did nothing.

    A delete that matched nothing is the interesting case — it means the
    id was stale, from another backend, or already removed — so it gets
    said out loud instead of being rounded to a cheerful "ok".
    """
    parts = [f"deleted {deleted} memor{'y' if deleted == 1 else 'ies'}"]
    if requested:
        parts.append(f"({requested} id{'s' if requested != 1 else ''} requested)")
    if deleted < requested:
        missed = requested - deleted
        parts.append(
            f"— {missed} id{'s' if missed != 1 else ''} matched no row "
            f"(already deleted, or from a different store)"
        )
    if rejected:
        shown = ", ".join(rejected[:5])
        more = f" +{len(rejected) - 5} more" if len(rejected) > 5 else ""
        parts.append(f"; rejected {len(rejected)} malformed id(s): {shown}{more}")
    return " ".join(parts)
