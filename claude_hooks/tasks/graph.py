"""The task graph across projects (ch-28 phase 1, docs/PLAN-task-graph.md).

A task only ever writes its own project's files. An edge into another
project is stored on our side (``depends``, or a typed link in
``links``) and read back through the host index, which holds every
project's rows; the reverse view (who depends on me, my children, who
relates to me) is computed here, never written into the other project.

The prefix is the global namespace: ``task_projects`` keeps prefixes
unique, so ``op-347`` names opencoti's task 347 from anywhere.

An id that cannot be resolved is blocking, never done: an unknown
prefix, an unknown task or an unreachable index all leave the
dependency open, with the reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from claude_hooks.tasks.model import OPEN_STATUSES, split_id

#: Typed links whose values are task ids. Only ``depends`` blocks; these
#: are structure and history. ``parent`` holds one id.
TASK_LINK_KINDS = ("parent", "discovered-from", "related", "caused-by",
                   "supersedes", "duplicate-of")

#: How each kind reads from the other end, for the reverse view.
REVERSE_NAMES = {
    "depends": "depended on by",
    "parent": "children",
    "discovered-from": "led to",
    "related": "related",
    "caused-by": "caused",
    "supersedes": "superseded by",
    "duplicate-of": "duplicated by",
}

#: A cycle search stops after this many nodes; a graph that large is
#: reported as unchecked rather than walked forever.
_MAX_NODES = 20000


@dataclass
class Ref:
    """What an id resolves to; ``reason`` is set when it does not."""
    id: str
    project: str = ""
    status: str = ""
    title: str = ""
    reason: str = ""

    @property
    def found(self) -> bool:
        return not self.reason

    @property
    def open(self) -> bool:
        """Blocking: unresolved, or resolved to an open task."""
        return not self.found or self.status in OPEN_STATUSES


@dataclass
class _Node:
    project: str
    id: str
    status: str = ""
    title: str = ""
    depends: list = field(default_factory=list)
    links: dict = field(default_factory=dict)
    #: Open gates (phase 3): it waits on something outside the graph.
    gates: list = field(default_factory=list)


def _ids(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    return [str(v).strip().lower() for v in value if str(v).strip()]


class Graph:
    """One read of every edge: ``local_rows`` for this project (its files
    are what this checkout sees), the index's rows for every other one."""

    def __init__(self, project: str, prefix: str,
                 local_rows: Iterable[dict], index=None):
        self.project = project
        self.prefix = prefix
        self.nodes: dict[str, _Node] = {}
        self.prefixes: dict[str, str] = {}
        #: Why other projects could not be read; "" when they were.
        self.index_error = "" if index is not None else (
            "no task index on this host")
        for r in local_rows:
            self._add(project, r)
        if index is not None:
            try:
                for p in index.projects():
                    self.prefixes[p["prefix"]] = p["project"]
                for r in index.edges():
                    if r["project"] != project:
                        self._add(r["project"], r)
            except Exception:
                self.index_error = "the task index is unavailable"
        self.prefixes[prefix] = project

    def _add(self, project: str, r: dict) -> None:
        tid = str(r.get("id") or "").lower()
        if not tid:
            return
        links = r.get("links") or {}
        self.nodes[tid] = _Node(
            project=project, id=tid, status=r.get("status") or "",
            title=r.get("title") or "", depends=_ids(r.get("depends")),
            links={k: _ids(v) for k, v in links.items()
                   if k in TASK_LINK_KINDS},
            gates=[str(g) for g in links.get("gates") or []])

    # ─── lookups ─────────────────────────────────────────────────────

    def ref(self, task_id: str) -> Ref:
        tid = str(task_id).strip().lower()
        n = self.nodes.get(tid)
        if n is not None:
            return Ref(tid, n.project, n.status, n.title)
        try:
            pre, _ = split_id(tid)
        except ValueError:
            return Ref(tid, reason=f"{task_id!r} is not a task id")
        if pre == self.prefix:
            return Ref(tid, self.project, reason=f"no task {tid}")
        if self.index_error:
            return Ref(tid, reason=self.index_error)
        project = self.prefixes.get(pre)
        if project is None:
            return Ref(tid, reason=f"no project uses the prefix {pre!r}")
        return Ref(tid, project, reason=f"no task {tid} in {project}")

    def is_local(self, task_id: str) -> bool:
        try:
            return split_id(str(task_id).lower())[0] == self.prefix
        except ValueError:
            return False

    def open_depends(self, depends: Iterable[str]) -> list[Ref]:
        """The blocking ones among ``depends``."""
        refs = [self.ref(d) for d in _ids(depends)]
        return [r for r in refs if r.open]

    def reverse(self, task_id: str, kind: str = "depends") -> list[_Node]:
        """Nodes whose ``kind`` edge points at ``task_id``, every project."""
        tid = task_id.lower()
        out = []
        for n in self.nodes.values():
            targets = n.depends if kind == "depends" else n.links.get(kind, [])
            if tid in targets:
                out.append(n)
        out.sort(key=lambda n: (n.project != self.project, n.project, n.id))
        return out

    def dependents_open(self, task_id: str) -> list[_Node]:
        return [n for n in self.reverse(task_id)
                if n.status in OPEN_STATUSES]

    def unblocked_by(self, task_id: str) -> list[_Node]:
        """Open tasks whose last open dependency is ``task_id``: closing it
        makes them ready. Assumes ``task_id`` is about to close."""
        tid = task_id.lower()
        out = []
        for n in self.dependents_open(tid):
            rest = [r for r in self.open_depends(n.depends) if r.id != tid]
            if not rest and not n.gates:
                out.append(n)
        return out

    # ─── cycles ──────────────────────────────────────────────────────

    def _edges(self, node_id: str, kind: str) -> list[str]:
        n = self.nodes.get(node_id)
        if n is None:
            return []
        return n.depends if kind == "depends" else n.links.get(kind, [])

    def cycle_through(self, task_id: str, new_targets: Iterable[str], *,
                      kind: str = "depends") -> Optional[list[str]]:
        """The path that adding ``task_id → new_targets`` would close into a
        cycle, ``[task_id, …, task_id]``, or None. Searches every project
        the graph holds; edges of unindexed tasks are unknown, so a cycle
        through one is not seen."""
        start = task_id.lower()
        seen: set[str] = set()
        for t in _ids(new_targets):
            stack: list[tuple[str, list[str]]] = [(t, [start, t])]
            while stack:
                node, path = stack.pop()
                if node == start:
                    return path
                if node in seen:
                    continue
                seen.add(node)
                if len(seen) > _MAX_NODES:
                    return None
                for nxt in self._edges(node, kind):
                    stack.append((nxt, path + [nxt]))
        return None


def describe(ref: Ref, *, here: str) -> str:
    """``op-347 (opencoti, active)`` / ``op-999 (unresolved: …)``."""
    if not ref.found:
        return f"{ref.id} (unresolved: {ref.reason})"
    where = f"{ref.project}, " if ref.project and ref.project != here else ""
    return f"{ref.id} ({where}{ref.status})"


# ─── views (phase 4) ─────────────────────────────────────────────────


def walk(graph: "Graph", start: str, direction: str = "both") -> set[str]:
    """Ids reachable from ``start`` along ``depends``: ``down`` = what it
    waits for, ``up`` = what waits for it, ``both``."""
    start = start.lower()
    out = {start}
    if direction in ("down", "both"):
        stack = [start]
        while stack:
            for d in graph._edges(stack.pop(), "depends"):
                if d not in out:
                    out.add(d)
                    stack.append(d)
    if direction in ("up", "both"):
        stack = [start]
        seen = {start}
        while stack:
            for n in graph.reverse(stack.pop()):
                if n.id not in seen:
                    seen.add(n.id)
                    out.add(n.id)
                    stack.append(n.id)
    return out


def layers(graph: "Graph", ids: Iterable[str], *,
           open_only: bool = True) -> list[list[str]]:
    """Execution order: layer 0 can start now (no open dependency inside
    the set), layer k waits only on lower layers; one layer can run in
    parallel. A dependency outside the set that is still open (or does
    not resolve) is not drawn but keeps its dependent out of layer 0, so
    a layer may be empty: the index is the layer number."""
    ids = [i.lower() for i in ids]
    if open_only:
        ids = [i for i in ids if graph.ref(i).open]
    keep = set(ids)
    level: dict[str, int] = {}

    def depth(i: str, trail: frozenset) -> int:
        if i in level:
            return level[i]
        if i in trail:                  # a cycle written by hand
            return 0
        deps = [d for d in graph._edges(i, "depends") if graph.ref(d).open]
        inside = [d for d in deps if d in keep]
        outside = len(deps) - len(inside)
        lv = 0
        for d in inside:
            lv = max(lv, depth(d, trail | {i}) + 1)
        if outside and lv == 0:
            lv = 1                      # waits on something not drawn
        level[i] = lv
        return lv
    for i in ids:
        depth(i, frozenset())
    if not level:
        return []
    out: list[list[str]] = [[] for _ in range(max(level.values()) + 1)]
    for i in sorted(level, key=lambda x: (graph.ref(x).project, x)):
        out[level[i]].append(i)
    return out


def text_view(graph: "Graph", ids: Iterable[str], *, here: str,
              open_only: bool = True) -> str:
    rows = layers(graph, ids, open_only=open_only)
    if not any(rows):
        return "Nothing open in that graph."
    out = []
    for k, lv in enumerate(rows):
        if not lv:
            continue
        head = ("Layer 0 — can start now" if k == 0
                else f"Layer {k} — after the layers above")
        out.append(f"{head} ({len(lv)}):")
        for i in lv:
            r = graph.ref(i)
            n = graph.nodes.get(i)
            deps = [d for d in (n.depends if n else []) if graph.ref(d).open]
            where = f" ({r.project})" if r.project and r.project != here else ""
            line = (f"  {i}{where} [{r.status or '?'}] · "
                    f"{r.title or r.reason}")
            if deps:
                line += " ← " + ", ".join(deps)
            if n is not None and n.gates:
                line += " · waits " + ", ".join(n.gates)
            out.append(line)
    return "\n".join(out)


def mermaid(graph: "Graph", ids: Iterable[str], *,
            open_only: bool = True) -> str:
    """A Mermaid flowchart: an arrow from each dependency to what waits
    for it, nodes grouped by project."""
    keep = [i.lower() for i in ids]
    if open_only:
        keep = [i for i in keep if graph.ref(i).open]
    keep_set = set(keep)

    def nid(i: str) -> str:
        return i.replace("-", "_")

    def label(i: str) -> str:
        r = graph.ref(i)
        t = (r.title or r.reason).replace('"', "'")
        if len(t) > 48:
            t = t[:47] + "…"
        return f'{nid(i)}["{i} · {t}<br/>{r.status or "?"}"]'
    by_project: dict[str, list[str]] = {}
    for i in keep:
        by_project.setdefault(graph.ref(i).project or "?", []).append(i)
    lines = ["flowchart TD"]
    for project, members in sorted(by_project.items()):
        lines.append(f"  subgraph {project.replace(' ', '_')}")
        lines += [f"    {label(i)}" for i in sorted(members)]
        lines.append("  end")
    for i in keep:
        for d in graph._edges(i, "depends"):
            if d in keep_set:
                lines.append(f"  {nid(d)} --> {nid(i)}")
    return "\n".join(lines)
