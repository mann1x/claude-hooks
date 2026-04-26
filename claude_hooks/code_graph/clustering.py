"""
Optional community detection over the call graph.

When the user has installed the ``code-graph[clustering]`` extra (which
pulls in ``python-louvain`` + ``networkx`` — both pure-Python, no native
deps), we can run Louvain modularity optimisation on the symbol graph
to find functional communities. The result is a per-node cluster id and
a "cohesion" score borrowed from gitnexus's terminology.

When the extra isn't installed we silently fall back to a coarse
filename-based clustering (everything in the same file is one cluster).
That's the floor — always returns *something*, never raises.

Public API:

    is_louvain_available() -> bool
    compute_clusters(graph) -> dict   # node_id -> cluster_id
    cluster_summary(graph, clusters) -> list[ClusterSummary]
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("claude_hooks.code_graph.clustering")

# Lazy probes.
_LOUVAIN_OK: Optional[bool] = None
_NX = None
_COMMUNITY_LOUVAIN = None


def is_louvain_available() -> bool:
    """True iff python-louvain + networkx import cleanly."""
    global _LOUVAIN_OK, _NX, _COMMUNITY_LOUVAIN
    if _LOUVAIN_OK is not None:
        return _LOUVAIN_OK
    try:
        import community as community_louvain  # python-louvain
        import networkx as nx
        _NX = nx
        _COMMUNITY_LOUVAIN = community_louvain
        _LOUVAIN_OK = True
    except ImportError:
        _LOUVAIN_OK = False
    return _LOUVAIN_OK


# ---------------------------------------------------------------------------
# Cluster summary
# ---------------------------------------------------------------------------

@dataclass
class ClusterSummary:
    cluster_id: int
    members: list[str] = field(default_factory=list)
    files: set[str] = field(default_factory=set)
    label: str = ""
    cohesion: float = 0.0   # internal_edges / (internal+external) ∈ [0,1]

    @property
    def size(self) -> int:
        return len(self.members)


# ---------------------------------------------------------------------------
# Clustering — Louvain with filename-fallback
# ---------------------------------------------------------------------------

def compute_clusters(graph: dict) -> dict[str, int]:
    """Return ``{node_id: cluster_id}`` for every function/method/class.

    Uses Louvain when available; otherwise falls back to grouping by
    file. Empty graph → empty dict.
    """
    nodes = graph.get("nodes") or []
    candidates = [
        n for n in nodes
        if n.get("type") in ("function", "method", "class")
    ]
    if not candidates:
        return {}

    if is_louvain_available():
        try:
            return _louvain_clusters(graph, candidates)
        except Exception as e:
            log.debug("Louvain failed (%s) — falling back to file clusters", e)
    return _file_clusters(candidates)


def _louvain_clusters(graph: dict, candidates: list[dict]) -> dict[str, int]:
    """Build an undirected NetworkX graph from calls + contains edges,
    then partition by modularity."""
    assert _NX is not None and _COMMUNITY_LOUVAIN is not None
    G = _NX.Graph()
    candidate_ids = {n["id"] for n in candidates}
    for nid in candidate_ids:
        G.add_node(nid)
    for e in graph.get("links") or ():
        if e.get("type") not in ("calls", "contains"):
            continue
        s, t = e.get("source"), e.get("target")
        if s in candidate_ids and t in candidate_ids and s != t:
            if G.has_edge(s, t):
                G[s][t]["weight"] = G[s][t].get("weight", 1) + 1
            else:
                G.add_edge(s, t, weight=1)

    if G.number_of_edges() == 0:
        # Fall back when there's no signal for Louvain to use
        return _file_clusters(candidates)

    partition = _COMMUNITY_LOUVAIN.best_partition(G, random_state=42)
    return {nid: int(cid) for nid, cid in partition.items()}


def _file_clusters(candidates: list[dict]) -> dict[str, int]:
    """One cluster per source file. Stable ordering for reproducibility."""
    seen_files: dict[str, int] = {}
    out: dict[str, int] = {}
    for n in sorted(candidates, key=lambda x: (x.get("file") or "", x.get("id") or "")):
        f = n.get("file") or ""
        if f not in seen_files:
            seen_files[f] = len(seen_files)
        out[n["id"]] = seen_files[f]
    return out


# ---------------------------------------------------------------------------
# Summarisation — produce ClusterSummary list with cohesion scores
# ---------------------------------------------------------------------------

def cluster_summary(graph: dict, clusters: dict[str, int]) -> list[ClusterSummary]:
    """Build summaries — members, dominant file label, cohesion score."""
    if not clusters:
        return []

    by_id = {n["id"]: n for n in (graph.get("nodes") or ()) if n.get("id")}
    by_cluster: defaultdict[int, list[str]] = defaultdict(list)
    for nid, cid in clusters.items():
        by_cluster[cid].append(nid)

    # Edge counters per cluster pair: internal vs crossing
    internal: defaultdict[int, int] = defaultdict(int)
    external: defaultdict[int, int] = defaultdict(int)
    for e in graph.get("links") or ():
        if e.get("type") not in ("calls", "contains"):
            continue
        s, t = e.get("source"), e.get("target")
        sc, tc = clusters.get(s), clusters.get(t)
        if sc is None or tc is None:
            continue
        if sc == tc:
            internal[sc] += 1
        else:
            external[sc] += 1
            external[tc] += 1

    summaries: list[ClusterSummary] = []
    for cid, members in by_cluster.items():
        files: set[str] = set()
        for mid in members:
            n = by_id.get(mid, {})
            f = n.get("file") or ""
            if f:
                files.add(f)
        # Label: most-common file basename → clusters get human-readable names
        label = _most_common_label(files) if files else f"cluster_{cid}"
        denom = internal[cid] + external[cid]
        cohesion = internal[cid] / denom if denom else 0.0
        summaries.append(ClusterSummary(
            cluster_id=cid,
            members=sorted(members),
            files=files,
            label=label,
            cohesion=cohesion,
        ))

    summaries.sort(key=lambda s: -s.size)
    return summaries


def _most_common_label(files: set[str]) -> str:
    """Pick a representative label from a set of files.

    For ``{a/b/c.py, a/b/d.py}`` → ``"a/b/"``  (common prefix, dir form).
    For ``{a/b/c.py}``           → ``"a/b/c.py"``.
    """
    if not files:
        return ""
    if len(files) == 1:
        return next(iter(files))
    sorted_files = sorted(files)
    common = sorted_files[0]
    for f in sorted_files[1:]:
        i = 0
        while i < len(common) and i < len(f) and common[i] == f[i]:
            i += 1
        common = common[:i]
    if not common:
        return f"({len(files)} files)"
    # Round to last directory boundary.
    if "/" in common:
        common = common.rsplit("/", 1)[0] + "/"
    return common or f"({len(files)} files)"


def format_cluster_report(summaries: list[ClusterSummary], *, top_n: int = 10) -> str:
    """Markdown summary of the top-N largest clusters."""
    if not summaries:
        return "_No clusters detected (graph empty?)._"
    out = ["# Cluster summary",
           "",
           f"_{len(summaries)} cluster(s) detected_ "
           f"({'Louvain' if is_louvain_available() else 'file-based fallback'}).",
           ""]
    for s in summaries[:top_n]:
        out.append(f"## `{s.label}` (cluster {s.cluster_id}, {s.size} nodes)")
        out.append("")
        out.append(f"- Cohesion: {s.cohesion:.2f}")
        out.append(f"- Files: {len(s.files)}")
        out.append("")
    if len(summaries) > top_n:
        out.append(f"_… + {len(summaries) - top_n} more clusters_")
    return "\n".join(out)
