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
