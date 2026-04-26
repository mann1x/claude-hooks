"""
Impact / blast-radius analysis over the code graph.

Given a target node id (or symbol name), walk the ``calls`` and
``contains`` edges *backwards* to find every transitive caller. The
result is the set of symbols whose behaviour could change if the
target's contract changes.

Borrowed in spirit from gitnexus's ``impact`` MCP tool, but kept
file-based and dependency-free: we read ``graphify-out/graph.json``
once, build adjacency dicts in memory, and do a plain BFS.

Public API:

    load_graph(root)                           -> dict
    resolve_target(graph, ref)                 -> Optional[str]   # id
    callers_of(graph, node_id, *, max_depth=N) -> list[(id, depth)]
    callees_of(graph, node_id, *, max_depth=N) -> list[(id, depth)]
    files_touched(graph, node_ids)             -> dict[file, [ids]]
    format_impact_report(graph, target, callers, callees) -> str

Never raises on bad input; missing nodes / unreachable refs return
empty results.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from pathlib import Path
from typing import Iterable, Optional

from claude_hooks.code_graph.detect import graph_json_path

log = logging.getLogger("claude_hooks.code_graph.impact")


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_graph(root: Path) -> dict:
    """Read graph.json. Returns ``{}`` on any failure."""
    gj = graph_json_path(root)
    if not gj.exists():
        return {}
    try:
        return json.loads(gj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.debug("load_graph failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Resolution — turn a user-friendly ref into a graph node id
# ---------------------------------------------------------------------------

def resolve_target(graph: dict, ref: str) -> Optional[str]:
    """Convert a name-like ref to a graph node id, or None.

    Accepts:
      - already-qualified id (``func:pkg.mod.foo``)
      - qualname (``pkg.mod.foo``)
      - bare name (``foo``) — when exactly one node matches
      - file path (``pkg/mod.py``) — picks the module node

    Ambiguous bare names return None — callers should print candidates
    instead of guessing.
    """
    if not ref:
        return None
    nodes = graph.get("nodes") or []
    if not nodes:
        return None

    # Direct id hit
    by_id = {n.get("id"): n for n in nodes}
    if ref in by_id:
        return ref

    # Qualname match
    for n in nodes:
        if n.get("qualname") == ref:
            return n.get("id")

    # File path → module node
    if ref.endswith((".py", ".js", ".ts", ".go", ".rs")):
        for n in nodes:
            if n.get("type") == "module" and n.get("file") == ref:
                return n.get("id")

    # Bare name — only when unambiguous
    bare = ref.rsplit(".", 1)[-1]
    candidates = [n["id"] for n in nodes if n.get("name") == bare
                  and n.get("type") in ("function", "method", "class")]
    if len(candidates) == 1:
        return candidates[0]
    return None


def name_candidates(graph: dict, ref: str) -> list[dict]:
    """Return all matching nodes for a ref. Used to print disambig hints."""
    bare = ref.rsplit(".", 1)[-1] if ref else ""
    if not bare:
        return []
    return [
        n for n in (graph.get("nodes") or [])
        if n.get("name") == bare
        and n.get("type") in ("function", "method", "class")
    ]


# ---------------------------------------------------------------------------
# Adjacency construction — cheap, computed once per call
# ---------------------------------------------------------------------------

def _build_adjacency(graph: dict) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (predecessors, successors) for the calls + contains subgraphs.

    For impact analysis we want *callers* (predecessors on the calls
    edge) and *containers* (predecessors on the contains edge — a
    function's containing class/module is also "affected" by edits to
    the function in the sense that you may want to know who else in
    that scope uses it).

    For callees we mirror — successors on calls (who does this fn call?)
    plus successors on contains (what's defined inside this scope?).
    """
    predecessors: defaultdict[str, list[str]] = defaultdict(list)
    successors: defaultdict[str, list[str]] = defaultdict(list)
    for e in graph.get("links") or ():
        if e.get("type") not in ("calls", "contains"):
            continue
        s, t = e.get("source"), e.get("target")
        if not s or not t:
            continue
        predecessors[t].append(s)
        successors[s].append(t)
    return dict(predecessors), dict(successors)


# ---------------------------------------------------------------------------
# BFS walks
# ---------------------------------------------------------------------------

def callers_of(
    graph: dict,
    node_id: str,
    *,
    max_depth: Optional[int] = 5,
    edge_types: tuple[str, ...] = ("calls",),
) -> list[tuple[str, int]]:
    """BFS upstream. Returns ``[(id, depth), ...]`` excluding the seed.

    ``max_depth=None`` means unbounded. ``edge_types`` narrows which
    edges to traverse — default is ``calls`` only (transitive callers);
    add ``contains`` to also include the containing class/module.
    """
    return _bfs(graph, node_id, max_depth=max_depth,
                direction="up", edge_types=edge_types)


def callees_of(
    graph: dict,
    node_id: str,
    *,
    max_depth: Optional[int] = 5,
    edge_types: tuple[str, ...] = ("calls",),
) -> list[tuple[str, int]]:
    """BFS downstream. Returns ``[(id, depth), ...]`` excluding the seed."""
    return _bfs(graph, node_id, max_depth=max_depth,
                direction="down", edge_types=edge_types)


def _bfs(
    graph: dict,
    node_id: str,
    *,
    max_depth: Optional[int],
    direction: str,
    edge_types: tuple[str, ...],
) -> list[tuple[str, int]]:
    if not node_id:
        return []
    # Build adjacency restricted to requested edge types.
    adj: defaultdict[str, list[str]] = defaultdict(list)
    for e in graph.get("links") or ():
        if e.get("type") not in edge_types:
            continue
        s, t = e.get("source"), e.get("target")
        if not s or not t:
            continue
        if direction == "up":
            adj[t].append(s)
        else:
            adj[s].append(t)

    seen: set[str] = {node_id}
    out: list[tuple[str, int]] = []
    queue: deque[tuple[str, int]] = deque([(node_id, 0)])
    while queue:
        cur, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for nxt in adj.get(cur, ()):
            if nxt in seen:
                continue
            seen.add(nxt)
            out.append((nxt, depth + 1))
            queue.append((nxt, depth + 1))
    return out


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def files_touched(graph: dict, node_ids: Iterable[str]) -> dict[str, list[str]]:
    """Group node ids by their containing file."""
    by_id = {n["id"]: n for n in graph.get("nodes") or () if n.get("id")}
    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for nid in node_ids:
        n = by_id.get(nid)
        if not n:
            continue
        f = n.get("file") or "<unknown>"
        grouped[f].append(nid)
    return dict(grouped)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def format_impact_report(
    graph: dict,
    target_id: str,
    callers: list[tuple[str, int]],
    callees: list[tuple[str, int]],
    *,
    cap: int = 50,
) -> str:
    """Markdown report mirroring gitnexus's ``impact`` shape."""
    by_id = {n["id"]: n for n in graph.get("nodes") or () if n.get("id")}
    target = by_id.get(target_id, {"id": target_id, "name": target_id, "file": "?", "line": 0})

    out: list[str] = []
    out.append(f"# Impact: `{target.get('qualname') or target.get('name') or target_id}`")
    out.append("")
    out.append(f"_Defined at `{target.get('file', '?')}:{target.get('line', 0)}`._")
    out.append("")

    # Callers (upstream — who would break if this changed)
    out.append(f"## Upstream callers ({len(callers)} total)")
    out.append("")
    if not callers:
        out.append("_No transitive callers in the project. Likely an entrypoint, "
                   "test fixture, or dead code._")
    else:
        files = files_touched(graph, [c[0] for c in callers])
        out.append(f"Spread across **{len(files)} file(s)**:")
        out.append("")
        for fname, ids in sorted(files.items(), key=lambda kv: -len(kv[1]))[:cap]:
            out.append(f"- `{fname}` — {len(ids)} symbol(s)")
            for nid in ids[:5]:
                node = by_id.get(nid, {})
                out.append(
                    f"  - `{node.get('qualname') or node.get('name') or nid}` "
                    f"(line {node.get('line', '?')})"
                )
            if len(ids) > 5:
                out.append(f"  - _… + {len(ids) - 5} more_")
        if len(files) > cap:
            out.append(f"\n_… + {len(files) - cap} more files_")

    # Callees (downstream — what this depends on)
    out.append("")
    out.append(f"## Downstream callees ({len(callees)} total)")
    out.append("")
    if not callees:
        out.append("_No outgoing calls in the project. Pure leaf or stdlib-only._")
    else:
        unique_files = sorted({by_id.get(cid, {}).get("file", "?")
                              for cid, _ in callees if cid in by_id})
        for fname in unique_files[:cap]:
            ids = [cid for cid, _ in callees if by_id.get(cid, {}).get("file") == fname]
            out.append(f"- `{fname}` — {len(ids)} symbol(s)")
        if len(unique_files) > cap:
            out.append(f"\n_… + {len(unique_files) - cap} more files_")

    return "\n".join(out)


def format_disambig(candidates: list[dict]) -> str:
    """Render a 'did you mean?' list when a bare name has multiple defs."""
    out = ["**Ambiguous reference** — multiple definitions match:", ""]
    for n in candidates:
        out.append(
            f"- `{n.get('qualname') or n.get('name')}` ({n.get('type')}) "
            f"at `{n.get('file')}:{n.get('line')}`"
        )
    out.append("")
    out.append("Re-run with the qualified name (e.g. `pkg.mod.func`) to disambiguate.")
    return "\n".join(out)
