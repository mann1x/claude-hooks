"""
Build the code-structure graph from a project tree.

Pass-1-only extraction: stdlib ``ast`` for Python files. Each parsed
file contributes:

- 1 ``module`` node  (id: ``module:dotted.path``)
- N ``function`` / ``class`` / ``method`` nodes
- Edges:
  - ``contains``  module → defs, class → methods
  - ``imports``   module → other module (best-effort dotted resolution)
  - ``calls``     caller def → callee def (intra-project only)

Output JSON uses NetworkX node-link format so graphify and ``networkx``
itself can both consume it without translation.

The whole module is dependency-free. A future tree-sitter backend would
add a parallel extractor under ``code_graph.tree_sitter_backend`` and
the merger here would just append its nodes/edges to the same graph.
"""

from __future__ import annotations

import ast
import datetime as _dt
import hashlib
import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

from claude_hooks.code_graph.detect import (
    CACHE_DIRNAME,
    EXTRACTABLE_EXTENSIONS,
    GRAPH_REPORT_FILENAME,
    IGNORED_DIRS,
    META_FILENAME,
    SUPPORTED_EXTENSIONS,
    graph_dir,
    graph_json_path,
    graph_report_path,
)

log = logging.getLogger("claude_hooks.code_graph.builder")

# Node id helpers — keep stable, parseable, prefix-grep-friendly.
def _module_id(dotted: str) -> str:
    return f"module:{dotted}"


def _func_id(dotted: str) -> str:
    return f"func:{dotted}"


def _class_id(dotted: str) -> str:
    return f"class:{dotted}"


def _dotted_for(root: Path, file: Path) -> str:
    """Convert a file path to a dotted module name relative to ``root``.

    ``root/claude_hooks/config.py`` → ``claude_hooks.config``
    ``root/claude_hooks/__init__.py`` → ``claude_hooks``
    """
    rel = file.resolve().relative_to(root.resolve())
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or rel.stem


def _iter_source_files(
    root: Path,
    *,
    ignored_dirs: frozenset[str] = IGNORED_DIRS,
    extensions: frozenset[str] = SUPPORTED_EXTENSIONS,
    max_files: int = 20000,
) -> Iterable[Path]:
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in extensions:
                continue
            seen += 1
            if seen > max_files:
                log.warning("code_graph: max_files cap %d reached, stopping walk", max_files)
                return
            yield Path(dirpath) / fn


class _PyExtractor(ast.NodeVisitor):
    """Walk a parsed Python module and emit nodes + edges."""

    def __init__(self, *, dotted: str, file_rel: str):
        self.dotted = dotted
        self.file_rel = file_rel
        self.scope_stack: list[str] = [dotted]  # current dotted scope for nested defs
        self.kind_stack: list[str] = ["module"]  # parallel: module / class / function
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        # Module node itself
        self.nodes.append({
            "id": _module_id(dotted),
            "type": "module",
            "name": dotted,
            "file": file_rel,
            "line": 1,
            "tag": "EXTRACTED",
        })

    # --- helpers ----------------------------------------------------------

    def _current_scope_id(self) -> str:
        kind = self.kind_stack[-1]
        scope = self.scope_stack[-1]
        if kind == "module":
            return _module_id(scope)
        if kind == "class":
            return _class_id(scope)
        return _func_id(scope)

    def _qualified(self, name: str) -> str:
        return f"{self.scope_stack[-1]}.{name}"

    def _docstring(self, node) -> Optional[str]:
        try:
            doc = ast.get_docstring(node)
        except TypeError:
            return None
        if not doc:
            return None
        # First non-empty line, capped — we don't need full prose in the graph.
        first = next((ln.strip() for ln in doc.splitlines() if ln.strip()), "")
        return first[:160] or None

    # --- visitors ---------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = self._qualified(node.name)
        cid = _class_id(qual)
        self.nodes.append({
            "id": cid,
            "type": "class",
            "name": node.name,
            "qualname": qual,
            "file": self.file_rel,
            "line": node.lineno,
            "tag": "EXTRACTED",
            "doc": self._docstring(node),
        })
        self.edges.append({
            "source": self._current_scope_id(),
            "target": cid,
            "type": "contains",
            "tag": "EXTRACTED",
        })
        self.scope_stack.append(qual)
        self.kind_stack.append("class")
        self.generic_visit(node)
        self.scope_stack.pop()
        self.kind_stack.pop()

    def _visit_func_like(self, node, *, is_async: bool) -> None:
        qual = self._qualified(node.name)
        fid = _func_id(qual)
        kind = "method" if self.kind_stack[-1] == "class" else "function"
        self.nodes.append({
            "id": fid,
            "type": kind,
            "name": node.name,
            "qualname": qual,
            "file": self.file_rel,
            "line": node.lineno,
            "tag": "EXTRACTED",
            "doc": self._docstring(node),
            "is_async": is_async,
        })
        self.edges.append({
            "source": self._current_scope_id(),
            "target": fid,
            "type": "contains",
            "tag": "EXTRACTED",
        })
        self.scope_stack.append(qual)
        self.kind_stack.append("function")
        self.generic_visit(node)
        self.scope_stack.pop()
        self.kind_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func_like(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func_like(node, is_async=True)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.edges.append({
                "source": _module_id(self.dotted),
                "target": _module_id(alias.name),
                "type": "imports",
                "tag": "EXTRACTED",
            })

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return  # bare ``from . import x`` — best left ambiguous
        # Resolve relative imports to a dotted path within the project.
        target = node.module
        if node.level:
            parts = self.dotted.split(".")
            base = parts[: max(0, len(parts) - node.level)]
            if target:
                base.append(target)
            target = ".".join(base) if base else target
        self.edges.append({
            "source": _module_id(self.dotted),
            "target": _module_id(target),
            "type": "imports",
            "tag": "EXTRACTED",
        })

    def visit_Call(self, node: ast.Call) -> None:
        # Only record calls when we're inside a function — module-level
        # calls are usually setup code that doesn't belong in the graph.
        if self.kind_stack[-1] != "function":
            self.generic_visit(node)
            return
        callee = _resolve_call_name(node.func)
        if callee:
            self.edges.append({
                "source": _func_id(self.scope_stack[-1]),
                # Resolution to a real ``func:dotted.path`` happens later
                # in the linker pass. For now we store the unresolved name.
                "target": f"unresolved:{callee}",
                "type": "calls",
                "tag": "EXTRACTED",
            })
        self.generic_visit(node)


def _resolve_call_name(func_node: ast.AST) -> Optional[str]:
    """Best-effort: turn a call's ``func`` node into a dotted name string."""
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        head = _resolve_call_name(func_node.value)
        return f"{head}.{func_node.attr}" if head else func_node.attr
    return None


def _link_calls(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Resolve ``unresolved:name`` call edges to real func/method nodes.

    Strategy: index every function/method by its bare name and by its
    qualified name. For each unresolved call, prefer a qualified-name
    match; fall back to bare-name match if exactly one exists. Drop
    edges that can't be resolved (calls into stdlib / 3rd-party libs).
    """
    by_qual: dict[str, str] = {}
    by_bare: defaultdict[str, list[str]] = defaultdict(list)
    for n in nodes:
        if n["type"] in ("function", "method"):
            by_qual[n["qualname"]] = n["id"]
            by_bare[n["name"]].append(n["id"])

    stats = {"resolved": 0, "dropped_external": 0, "ambiguous": 0}
    out: list[dict] = []
    for e in edges:
        if e["type"] != "calls" or not e["target"].startswith("unresolved:"):
            out.append(e)
            continue
        name = e["target"][len("unresolved:"):]
        target = by_qual.get(name)
        if target is None:
            candidates = by_bare.get(name.split(".")[-1], [])
            if len(candidates) == 1:
                target = candidates[0]
            elif len(candidates) > 1:
                stats["ambiguous"] += 1
                continue  # ambiguous — drop
        if target is None:
            stats["dropped_external"] += 1
            continue
        e2 = dict(e)
        e2["target"] = target
        # Mark inferred when we matched on bare name (uncertain).
        if target != by_qual.get(name):
            e2["tag"] = "INFERRED"
        out.append(e2)
        stats["resolved"] += 1
    return out, stats


def _file_sha(file: Path) -> str:
    h = hashlib.sha256()
    with open(file, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_graph(
    root: Path,
    *,
    max_files: int = 20000,
    incremental: bool = True,
) -> dict:
    """Walk ``root``, extract nodes/edges, write graph.json + report + meta.

    Returns a stats dict: ``{"files": N, "nodes": N, "edges": N, ...}``.
    Idempotent — safe to call repeatedly. Incremental when the cache is
    intact, full rebuild when the cache is missing or corrupt.
    """
    out_dir = graph_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / CACHE_DIRNAME
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Cache layout: cache/<sha>.json  →  {"nodes": [...], "edges": [...]}
    # Plus cache/manifest.json: {file_rel: sha}.
    manifest_path = cache_dir / "manifest.json"
    prev_manifest: dict[str, str] = {}
    if incremental and manifest_path.exists():
        try:
            prev_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(prev_manifest, dict):
                prev_manifest = {}
        except (OSError, json.JSONDecodeError):
            prev_manifest = {}

    new_manifest: dict[str, str] = {}
    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    by_lang: defaultdict[str, int] = defaultdict(int)
    parsed_files = 0
    cached_hits = 0
    parse_errors: list[str] = []

    # Decide which extensions we can actually extract from this run.
    from claude_hooks.code_graph import tree_sitter_backend as _ts
    ts_extensions = _ts.supported_extensions()
    extractable = EXTRACTABLE_EXTENSIONS | ts_extensions

    for file in _iter_source_files(root, max_files=max_files):
        ext = file.suffix.lower()
        by_lang[ext] += 1
        rel = str(file.resolve().relative_to(root.resolve()))

        if ext not in extractable:
            continue  # supported (counted) but no parser available

        try:
            sha = _file_sha(file)
        except OSError as e:
            parse_errors.append(f"{rel}: {e}")
            continue
        new_manifest[rel] = sha

        cache_file = cache_dir / f"{sha}.json"
        if incremental and prev_manifest.get(rel) == sha and cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                all_nodes.extend(cached.get("nodes", []))
                all_edges.extend(cached.get("edges", []))
                cached_hits += 1
                parsed_files += 1
                continue
            except (OSError, json.JSONDecodeError):
                pass  # fall through to fresh parse

        dotted = _dotted_for(root, file)

        if ext == ".py":
            try:
                src = file.read_text(encoding="utf-8")
                tree = ast.parse(src, filename=rel)
            except (OSError, SyntaxError, UnicodeDecodeError) as e:
                parse_errors.append(f"{rel}: {e}")
                continue
            ext_obj = _PyExtractor(dotted=dotted, file_rel=rel)
            ext_obj.visit(tree)
            file_nodes, file_edges = ext_obj.nodes, ext_obj.edges
        else:
            try:
                raw = file.read_bytes()
            except OSError as e:
                parse_errors.append(f"{rel}: {e}")
                continue
            file_nodes, file_edges = _ts.extract_file(
                file=file, rel=rel, dotted=dotted, text=raw,
            )
            if not file_nodes:
                parse_errors.append(f"{rel}: tree-sitter extraction empty")
                continue

        try:
            cache_file.write_text(
                json.dumps({"nodes": file_nodes, "edges": file_edges}),
                encoding="utf-8",
            )
        except OSError:
            pass  # cache write failure is non-fatal

        all_nodes.extend(file_nodes)
        all_edges.extend(file_edges)
        parsed_files += 1

    # Resolve call edges from name strings to real node ids.
    all_edges, link_stats = _link_calls(all_nodes, all_edges)

    # Drop import edges that point at modules outside the project, since
    # we can't navigate to them anyway and they bloat the graph.
    project_modules = {n["id"] for n in all_nodes if n["type"] == "module"}
    pruned_edges = []
    external_imports = 0
    for e in all_edges:
        if e["type"] == "imports" and e["target"] not in project_modules:
            external_imports += 1
            continue
        pruned_edges.append(e)
    all_edges = pruned_edges

    # NetworkX node-link payload (directed). graphify reads the same shape.
    payload = {
        "directed": True,
        "multigraph": False,
        "graph": {
            "generated_by": "claude-hooks",
            "generated_at": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "stats": {
                "files_parsed": parsed_files,
                "files_cached": cached_hits,
                "nodes": len(all_nodes),
                "edges": len(all_edges),
                "calls_resolved": link_stats["resolved"],
                "calls_external": link_stats["dropped_external"],
                "calls_ambiguous": link_stats["ambiguous"],
                "imports_external": external_imports,
                "by_language": dict(by_lang),
                "parse_errors": len(parse_errors),
            },
        },
        "nodes": all_nodes,
        "links": all_edges,
    }

    # Write graph.json + manifest + meta + report atomically-ish.
    _atomic_write(graph_json_path(root), json.dumps(payload, indent=2))
    _atomic_write(manifest_path, json.dumps(new_manifest, indent=2, sort_keys=True))
    _atomic_write(out_dir / META_FILENAME, json.dumps({
        "managed_by": "claude-hooks",
        "version": 1,
        "generated_at": payload["graph"]["generated_at"],
    }, indent=2))
    report = render_report(payload)
    _atomic_write(graph_report_path(root), report)

    return payload["graph"]["stats"]


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Report rendering — the human (and model) readable summary.
# ---------------------------------------------------------------------------

def render_report(payload: dict, *, top_n: int = 10) -> str:
    """Render GRAPH_REPORT.md from a node-link payload.

    Sections (mirroring graphify):
      - God Nodes — top-N by total degree
      - Modules — biggest weakly-connected components by node count
      - Entrypoints — funcs with no incoming ``calls`` edge that look
        like CLI mains or tests
      - Stats — what got built, what we skipped
    """
    nodes = payload["nodes"]
    edges = payload["links"]
    stats = payload["graph"]["stats"]

    in_deg: defaultdict[str, int] = defaultdict(int)
    out_deg: defaultdict[str, int] = defaultdict(int)
    callers_of: defaultdict[str, list[str]] = defaultdict(list)
    for e in edges:
        in_deg[e["target"]] += 1
        out_deg[e["source"]] += 1
        if e["type"] == "calls":
            callers_of[e["target"]].append(e["source"])

    by_id = {n["id"]: n for n in nodes}

    # God nodes: highest combined degree among funcs/methods/classes.
    candidates = [
        n for n in nodes
        if n["type"] in ("function", "method", "class")
    ]
    god = sorted(
        candidates,
        key=lambda n: in_deg[n["id"]] + out_deg[n["id"]],
        reverse=True,
    )[:top_n]

    # Modules: rank by # of contained defs (proxy for "important module").
    module_size: defaultdict[str, int] = defaultdict(int)
    for e in edges:
        if e["type"] == "contains" and e["source"].startswith("module:"):
            module_size[e["source"]] += 1
    top_modules = sorted(module_size.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    # Entrypoints: functions with 0 callers that look like main/cli/test.
    entry_keywords = ("main", "cli", "run", "handle")
    entrypoints = [
        n for n in nodes
        if n["type"] in ("function", "method")
        and in_deg[n["id"]] == 0
        and any(kw in n["name"].lower() for kw in entry_keywords)
    ][:top_n]

    out: list[str] = []
    out.append("# Project graph report")
    out.append("")
    out.append(f"> Generated by claude-hooks code_graph at {payload['graph']['generated_at']}")
    out.append(f"> {stats['files_parsed']} files parsed "
               f"({stats['files_cached']} from cache), "
               f"{stats['nodes']} nodes, {stats['edges']} edges.")
    out.append("")

    out.append("## God nodes (top by call/contains degree)")
    out.append("")
    if not god:
        out.append("_(no functions or classes parsed yet)_")
    else:
        for n in god:
            ins, outs = in_deg[n["id"]], out_deg[n["id"]]
            doc = (n.get("doc") or "").strip()
            doc_part = f" — {doc}" if doc else ""
            out.append(f"- `{n['id']}` ({n['file']}:{n['line']}) "
                       f"— {ins} in / {outs} out{doc_part}")
    out.append("")

    out.append("## Top modules (by direct defs)")
    out.append("")
    if not top_modules:
        out.append("_(no modules parsed yet)_")
    else:
        for mod_id, sz in top_modules:
            mn = by_id.get(mod_id, {})
            file = mn.get("file", "")
            out.append(f"- `{mod_id}` ({file}) — {sz} defs")
    out.append("")

    out.append("## Likely entrypoints")
    out.append("")
    if not entrypoints:
        out.append("_(none detected — heuristic looks for `main`/`cli`/`run`/`handle` "
                   "with no callers)_")
    else:
        for n in entrypoints:
            out.append(f"- `{n['id']}` ({n['file']}:{n['line']})")
    out.append("")

    out.append("## How to use this graph")
    out.append("")
    out.append(
        "- For \"who calls X?\" or \"where is Y defined?\", search "
        f"`{GRAPH_REPORT_FILENAME}` first; fall back to `graph.json` "
        "for full structure (node-link JSON, NetworkX-compatible)."
    )
    out.append("- God nodes are the highest-traffic defs — touch with care.")
    out.append("- Top modules are where the most logic lives — start orientation there.")
    out.append("")

    by_lang = stats.get("by_language", {})
    if by_lang:
        out.append("## File counts by extension")
        out.append("")
        for ext, n in sorted(by_lang.items(), key=lambda kv: kv[1], reverse=True):
            note = "" if ext in EXTRACTABLE_EXTENSIONS else "  _(counted, not yet parsed)_"
            out.append(f"- `{ext}` — {n}{note}")
        out.append("")

    return "\n".join(out)
