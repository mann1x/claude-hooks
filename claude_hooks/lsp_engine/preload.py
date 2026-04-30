"""Adaptive preload from the built-in code graph.

Decision 3 of ``docs/PLAN-lsp-engine.md``: on daemon startup, the
engine eagerly ``did_open``s the top-N most-imported files instead
of either lazy-loading everything (cold-start tax on first edit) or
preloading the whole project (30-60 s and 200+ MB on a large repo).

The "hotness" signal is in-degree on the ``imports`` edges in
``graphify-out/graph.json`` — a file imported by 50 other modules
is far more likely to be touched in the next 5 minutes than a
file imported by 0. This module is the bridge: read the graph,
rank, and ``did_open`` the top slice into a provided ``Engine``.

Soft-fail by design. If ``graph.json`` is absent (the user hasn't
built the code graph) or unreadable, ``preload_engine()`` does
nothing and returns 0. The engine still works in lazy mode.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.lsp_engine.preload")


GRAPH_JSON_REL_PATH = Path("graphify-out") / "graph.json"


def graph_json_path_for(project_root: str | os.PathLike) -> Path:
    return Path(project_root) / GRAPH_JSON_REL_PATH


def rank_files_by_in_degree(
    graph_json_path: str | os.PathLike,
) -> list[tuple[str, int]]:
    """Read ``graph.json`` and return ``[(rel_file_path, in_degree), …]``
    sorted by in-degree desc.

    The graph is in NetworkX node-link format: ``{"graph": {"nodes":
    [...], "edges": [...]}}``. Each module node carries a relative
    ``file`` path; ``imports`` edges target other module ids. We
    count in-degree per module id, then map id → file via the node
    list. Files with zero imports still appear (in-degree 0) so the
    caller can pick them up if they ask for more than the
    most-imported set.
    """
    p = Path(graph_json_path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("preload: cannot read %s: %s", p, e)
        return []

    graph_block = data.get("graph") or data
    nodes = graph_block.get("nodes") or []
    edges = graph_block.get("edges") or []

    id_to_file: dict[str, str] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("type") != "module":
            continue
        node_id = n.get("id")
        file_rel = n.get("file")
        if isinstance(node_id, str) and isinstance(file_rel, str) and file_rel:
            id_to_file[node_id] = file_rel

    in_deg: dict[str, int] = defaultdict(int)
    for e in edges:
        if not isinstance(e, dict) or e.get("type") != "imports":
            continue
        target = e.get("target")
        if isinstance(target, str):
            in_deg[target] += 1

    # Combine: every module node gets a row (degree 0 if unreferenced).
    rows: list[tuple[str, int]] = []
    for node_id, file_rel in id_to_file.items():
        rows.append((file_rel, in_deg.get(node_id, 0)))

    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def preload_engine(
    engine,
    project_root: str | os.PathLike,
    *,
    max_files: int = 200,
    graph_path: Optional[str | os.PathLike] = None,
    extension_filter: Optional[set[str]] = None,
) -> int:
    """Read content from disk for the top-``max_files`` hot files and
    ``did_open`` them into ``engine``. Returns the number actually
    opened (a file is skipped silently if it's missing on disk, the
    engine has no LSP for its extension, or reading raises OSError).

    ``extension_filter`` (lowercased extensions, no leading dot) caps
    preload to specific languages — useful when the daemon has servers
    for only a subset of what's in the graph.
    """
    root = Path(project_root)
    graph_p = Path(graph_path) if graph_path else graph_json_path_for(root)
    rows = rank_files_by_in_degree(graph_p)
    if not rows:
        log.info(
            "preload: no graph.json at %s — engine starts in lazy mode",
            graph_p,
        )
        return 0

    opened = 0
    attempted = 0
    for file_rel, _degree in rows:
        if opened >= max_files:
            break
        attempted += 1
        if extension_filter is not None:
            ext = Path(file_rel).suffix.lower().lstrip(".")
            if ext not in extension_filter:
                continue
        abs_path = (root / file_rel).resolve()
        if not abs_path.is_file():
            continue
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log.debug("preload: skip %s: %s", abs_path, e)
            continue
        try:
            if engine.did_open(abs_path, content):
                opened += 1
        except Exception:  # pragma: no cover — defensive
            log.exception("preload: did_open failed for %s", abs_path)

    log.info(
        "preload: opened %d/%d files (top-%d by in-degree)",
        opened, attempted, max_files,
    )
    return opened
