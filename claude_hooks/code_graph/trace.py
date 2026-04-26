"""
Process tracing — call chains from a chosen entrypoint.

Borrowed from gitnexus's "process tracing" idea: pick an entrypoint
(a CLI's ``main``, an HTTP handler, a hook handler, ...) and walk
forward through the ``calls`` graph to enumerate every function it
might transitively reach. Useful for answers like:

  "How does request X flow through this service?"
  "What does our session_start hook actually do?"
  "Show me the work my CLI's main() can fan out to."

Public API:

    enumerate_entrypoints(graph, *, keywords=...) -> list[node]
    trace(graph, root_id, *, max_depth=8, max_nodes=200) -> Trace
    format_trace_report(graph, trace) -> str

Trace returns paths (as lists of node ids) up to ``max_depth`` levels
deep, capped at ``max_nodes`` to keep monorepos readable. Cycles are
broken — each node appears at most once per path.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from claude_hooks.code_graph.impact import callees_of

log = logging.getLogger("claude_hooks.code_graph.trace")


_DEFAULT_ENTRY_KEYWORDS = ("main", "cli", "run", "handle", "serve", "start")


@dataclass
class TraceNode:
    """One step in the trace — a node + its depth + its parents."""
    node_id: str
    depth: int
    parents: list[str] = field(default_factory=list)


@dataclass
class Trace:
    """Forward call-chain trace from a root entrypoint."""
    root: str
    nodes: dict[str, TraceNode] = field(default_factory=dict)
    truncated: bool = False
    max_depth_used: int = 0

    @property
    def total(self) -> int:
        return len(self.nodes)


# ---------------------------------------------------------------------------
# Entrypoint discovery
# ---------------------------------------------------------------------------

def enumerate_entrypoints(
    graph: dict,
    *,
    keywords: tuple[str, ...] = _DEFAULT_ENTRY_KEYWORDS,
    require_no_callers: bool = True,
) -> list[dict]:
    """Heuristic: name-keyword match + (optionally) zero in-degree on
    ``calls`` edges.

    A node like ``main`` that nothing in the project calls is *probably*
    an entrypoint (CLI, HTTP handler, hook). A node like ``main`` that's
    called by tests isn't — exclude it via the in-degree gate by
    default, or override with ``require_no_callers=False`` to include
    test fixtures and similar.
    """
    in_deg: defaultdict[str, int] = defaultdict(int)
    for e in graph.get("links") or ():
        if e.get("type") == "calls":
            in_deg[e.get("target")] += 1

    out: list[dict] = []
    for n in graph.get("nodes") or ():
        if n.get("type") not in ("function", "method"):
            continue
        name = (n.get("name") or "").lower()
        if not any(kw in name for kw in keywords):
            continue
        if require_no_callers and in_deg.get(n.get("id"), 0) > 0:
            continue
        out.append(n)
    return out


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------

def trace(
    graph: dict,
    root_id: str,
    *,
    max_depth: int = 8,
    max_nodes: int = 200,
) -> Trace:
    """Forward BFS from ``root_id`` over ``calls`` edges. Returns a Trace."""
    result = Trace(root=root_id)
    if not root_id:
        return result

    nodes_by_id = {n["id"]: n for n in (graph.get("nodes") or ()) if n.get("id")}
    if root_id not in nodes_by_id:
        return result

    succ: defaultdict[str, list[str]] = defaultdict(list)
    for e in graph.get("links") or ():
        if e.get("type") != "calls":
            continue
        s, t = e.get("source"), e.get("target")
        if s and t:
            succ[s].append(t)

    result.nodes[root_id] = TraceNode(node_id=root_id, depth=0)
    queue: deque[tuple[str, int]] = deque([(root_id, 0)])
    while queue:
        cur, d = queue.popleft()
        if d >= max_depth:
            continue
        for child in succ.get(cur, ()):
            if len(result.nodes) >= max_nodes:
                result.truncated = True
                return result
            existing = result.nodes.get(child)
            if existing is None:
                result.nodes[child] = TraceNode(
                    node_id=child, depth=d + 1, parents=[cur]
                )
                queue.append((child, d + 1))
                result.max_depth_used = max(result.max_depth_used, d + 1)
            else:
                if cur not in existing.parents:
                    existing.parents.append(cur)
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def format_trace_report(graph: dict, t: Trace, *, cap_per_depth: int = 12) -> str:
    """Markdown report grouping nodes by depth from the root."""
    by_id = {n["id"]: n for n in (graph.get("nodes") or ()) if n.get("id")}
    root = by_id.get(t.root, {"id": t.root, "name": t.root, "file": "?", "line": 0})

    out: list[str] = []
    out.append(f"# Process trace: `{root.get('qualname') or root.get('name') or t.root}`")
    out.append("")
    out.append(f"_Entry at `{root.get('file', '?')}:{root.get('line', 0)}`._")
    out.append("")

    if t.total <= 1:
        out.append("_No outgoing calls into the project graph. Either a "
                   "stdlib-only leaf or an entrypoint that immediately "
                   "delegates to an external library._")
        return "\n".join(out)

    out.append(f"**{t.total - 1} callable(s) reachable** "
               f"(max depth {t.max_depth_used}"
               f"{', truncated' if t.truncated else ''}).")
    out.append("")

    # Group by depth
    by_depth: defaultdict[int, list[str]] = defaultdict(list)
    for nid, tn in t.nodes.items():
        if nid == t.root:
            continue
        by_depth[tn.depth].append(nid)

    for depth in sorted(by_depth):
        ids = by_depth[depth]
        out.append(f"## Depth {depth} ({len(ids)} node(s))")
        out.append("")
        for nid in sorted(ids)[:cap_per_depth]:
            n = by_id.get(nid, {})
            out.append(
                f"- `{n.get('qualname') or n.get('name') or nid}` "
                f"({n.get('file', '?')}:{n.get('line', '?')})"
            )
        if len(ids) > cap_per_depth:
            out.append(f"- _… + {len(ids) - cap_per_depth} more at this depth_")
        out.append("")

    return "\n".join(out)
