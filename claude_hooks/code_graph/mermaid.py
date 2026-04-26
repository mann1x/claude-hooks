"""
Mermaid module-map renderer for the code graph.

Borrowed from gitnexus's ``generate_map`` MCP prompt — produces a small
diagram of the top-N modules and the import edges between them. Suitable
for PR descriptions, READMEs, or just orienting yourself in an unfamiliar
project.

Public API:

    render_module_map(graph, *, top_n=15, max_edges=80) -> str
    render_subgraph(graph, root_id, *, depth=2) -> str

Both return plain Mermaid markdown (no ```mermaid fence — the caller
adds it if their target wants one). Empty/no-edge graphs render a
``flowchart LR`` shell with a single placeholder node so downstream
tooling never gets a malformed diagram.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Iterable

log = logging.getLogger("claude_hooks.code_graph.mermaid")

# Mermaid node ids: alphanumeric + underscore. Anything else gets stripped.
_SAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def _safe_id(s: str, *, prefix: str = "n") -> str:
    """Make a Mermaid-legal id. Length capped to keep diagrams readable."""
    cleaned = _SAFE_RE.sub("_", s)[:48]
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"{prefix}_{cleaned}"
    return cleaned


def _quote_label(s: str) -> str:
    """Mermaid label — quoted, escape inner quotes."""
    return '"' + s.replace('"', "&quot;") + '"'


# ---------------------------------------------------------------------------
# Module map
# ---------------------------------------------------------------------------

def render_module_map(
    graph: dict,
    *,
    top_n: int = 15,
    max_edges: int = 80,
) -> str:
    """Top-N modules + the imports edges between them.

    Modules are ranked by total degree (imports in + imports out + direct
    defs). Edges include only import relations *within* the top-N set,
    so the diagram stays readable even on large projects.
    """
    nodes = graph.get("nodes") or []
    edges = graph.get("links") or []

    # Score each module by activity
    in_imports: defaultdict[str, int] = defaultdict(int)
    out_imports: defaultdict[str, int] = defaultdict(int)
    contains_count: defaultdict[str, int] = defaultdict(int)
    for e in edges:
        et = e.get("type")
        s, t = e.get("source"), e.get("target")
        if not s or not t:
            continue
        if et == "imports":
            out_imports[s] += 1
            in_imports[t] += 1
        elif et == "contains" and s.startswith("module:"):
            contains_count[s] += 1

    module_nodes = [n for n in nodes if n.get("type") == "module"]
    if not module_nodes:
        return _empty_diagram("no modules in graph")

    def _score(m: dict) -> int:
        mid = m["id"]
        return contains_count[mid] + in_imports[mid] + out_imports[mid]

    top = sorted(module_nodes, key=_score, reverse=True)[:top_n]
    top_ids = {m["id"] for m in top}

    # Build edge list restricted to the top-N
    pair_count: defaultdict[tuple[str, str], int] = defaultdict(int)
    for e in edges:
        if e.get("type") != "imports":
            continue
        s, t = e.get("source"), e.get("target")
        if s in top_ids and t in top_ids and s != t:
            pair_count[(s, t)] += 1

    # Sort edges by traffic (most-imported first), cap to max_edges
    edge_pairs = sorted(pair_count.items(), key=lambda kv: -kv[1])[:max_edges]

    # ----- emit -----
    lines = ["flowchart LR"]
    for m in top:
        mid = m["id"]
        nid = _safe_id(mid)
        size = contains_count[mid]
        label = f"{m.get('name', mid)}<br/><small>{size} defs</small>" if size else m.get("name", mid)
        lines.append(f"    {nid}[{_quote_label(label)}]")
    for (s, t), count in edge_pairs:
        sid, tid = _safe_id(s), _safe_id(t)
        if count > 1:
            lines.append(f"    {sid} -->|{count}| {tid}")
        else:
            lines.append(f"    {sid} --> {tid}")

    if len(pair_count) > max_edges:
        lines.append(
            f"    %% ... + {len(pair_count) - max_edges} more import edges "
            f"truncated for readability"
        )

    return "\n".join(lines) + "\n"


def render_subgraph(
    graph: dict,
    root_id: str,
    *,
    depth: int = 2,
    max_nodes: int = 30,
) -> str:
    """Local Mermaid view: ``root_id`` + everything reachable within ``depth``
    hops on calls/contains edges. Useful for "show me the world around X".
    """
    nodes_by_id = {n["id"]: n for n in (graph.get("nodes") or ()) if n.get("id")}
    if root_id not in nodes_by_id:
        return _empty_diagram(f"unknown node: {root_id}")

    succ: defaultdict[str, list[str]] = defaultdict(list)
    pred: defaultdict[str, list[str]] = defaultdict(list)
    for e in graph.get("links") or ():
        if e.get("type") not in ("calls", "contains"):
            continue
        s, t = e.get("source"), e.get("target")
        if s and t:
            succ[s].append(t)
            pred[t].append(s)

    seen: set[str] = {root_id}
    edges: list[tuple[str, str]] = []
    frontier = [(root_id, 0)]
    while frontier:
        cur, d = frontier.pop()
        if d >= depth or len(seen) >= max_nodes:
            continue
        for adj_dict in (succ, pred):
            for nxt in adj_dict.get(cur, ()):
                if nxt not in seen:
                    if len(seen) >= max_nodes:
                        break
                    seen.add(nxt)
                    frontier.append((nxt, d + 1))
                edge = (cur, nxt) if adj_dict is succ else (nxt, cur)
                if edge not in edges:
                    edges.append(edge)

    lines = ["flowchart TD"]
    for nid in seen:
        n = nodes_by_id.get(nid, {"id": nid, "name": nid, "type": "unknown"})
        sid = _safe_id(nid)
        label = n.get("qualname") or n.get("name") or nid
        # Highlight the root
        if nid == root_id:
            lines.append(f"    {sid}[{_quote_label(label)}]:::root")
        else:
            lines.append(f"    {sid}[{_quote_label(label)}]")
    for s, t in edges:
        if s in seen and t in seen:
            lines.append(f"    {_safe_id(s)} --> {_safe_id(t)}")
    lines.append("    classDef root fill:#fde,stroke:#a04,stroke-width:2px")
    return "\n".join(lines) + "\n"


def _empty_diagram(msg: str) -> str:
    return f"flowchart LR\n    empty[{_quote_label(msg)}]\n"
