"""Look up the function/class/method enclosing a given file:line.

A thin, pure-read query layer over the on-disk
``graphify-out/graph.json`` artifact. Loads the index lazily (mtime-
cached, same process-global pattern as
:mod:`claude_hooks.code_graph.symbol_lookup`) and answers:

    "what symbol contains <file>:<line>?"

The primary user is :mod:`consultants.engine.citation_linter`, which
needs to validate ``path:line`` citations in synthesized answers
against AST ground truth without re-parsing every cited file on
every lint pass. The graph build (when present) is the fast path;
the linter falls back to on-demand :mod:`ast` parsing when:

* The graph hasn't been built (``graph.json`` missing).
* The graph predates the ``end_line`` field (extractor version < 2).
* The file isn't covered by the graph (non-Python, or outside the
  builder's walk root).

Returns ``None`` in every "I don't know" case — callers MUST treat
``None`` as "fall back to your own check", not "no symbol at that
line". The graph is advisory, never authoritative.

Public API:

* :func:`enclosing_symbol_at` — given a project root, a file path
  relative to that root, and a 1-indexed line number, return the
  innermost function/class/method name whose ``[line, end_line]``
  span contains the line, or ``None``.

* :func:`graph_covers_file` — quick "is this file in the graph?"
  check so callers can decide between graph-vs-fallback once per
  cited file (instead of per-line).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

from claude_hooks.code_graph.detect import graph_json_path

log = logging.getLogger("claude_hooks.code_graph.enclosing")

# Per-process cache keyed by graph.json absolute path: each entry
# stores (mtime, {file_rel: [(start, end, name), ...]}) so the
# linter pays the parse cost once per graph rebuild.
_FILE_INDEX_CACHE: dict[
    str, tuple[float, dict[str, list[tuple[int, int, str]]]]
] = {}


def _load_file_index(
    root: Path,
) -> dict[str, list[tuple[int, int, str]]]:
    """Return ``{file_rel: [(start, end, name), ...]}`` for the graph
    at ``root``, or ``{}`` when the graph is missing / malformed /
    pre-end_line.

    Caches in-process by mtime so repeat lint passes are O(1) after
    the first hit. ``end_line`` < ``line`` (stale extractor v1
    rows) → that node is dropped from the index. A file whose nodes
    ALL got dropped this way is effectively absent from the
    returned dict, which makes :func:`graph_covers_file` return
    False and callers correctly fall back to ast.parse.
    """
    gj = graph_json_path(root)
    if not gj.exists():
        return {}
    try:
        mtime = gj.stat().st_mtime
    except OSError:
        return {}

    cache_key = str(gj)
    cached = _FILE_INDEX_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        payload = json.loads(gj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.debug("enclosing: cannot load %s: %s", gj, e)
        _FILE_INDEX_CACHE[cache_key] = (mtime, {})
        return {}

    by_file: defaultdict[str, list[tuple[int, int, str]]] = defaultdict(
        list
    )
    for n in payload.get("nodes") or ():
        if n.get("type") not in ("function", "method", "class"):
            continue
        file_rel = n.get("file") or ""
        if not file_rel:
            continue
        try:
            start = int(n.get("line") or 0)
            end = int(n.get("end_line") or 0)
        except (TypeError, ValueError):
            continue
        # Skip degenerate / legacy rows. end_line missing or zero
        # means an extractor-v1 node — those have no range info and
        # would only confuse the lookup; drop them so the file falls
        # through to the ast.parse fallback in the caller.
        if start < 1 or end < start:
            continue
        name = n.get("name") or ""
        if not name:
            continue
        by_file[file_rel].append((start, end, name))

    result = dict(by_file)
    _FILE_INDEX_CACHE[cache_key] = (mtime, result)
    return result


def clear_cache() -> None:
    """Drop the in-process index cache (test helper)."""
    _FILE_INDEX_CACHE.clear()


def graph_covers_file(root: Path, file_rel: str) -> bool:
    """True iff the on-disk graph has any function/class/method nodes
    for ``file_rel`` with valid ``end_line`` info.

    Callers use this to decide once per file whether to trust the
    graph or fall back to ast.parse, instead of paying the lookup
    cost per cite.
    """
    index = _load_file_index(root)
    return bool(index.get(file_rel))


def enclosing_symbol_at(
    root: Path, file_rel: str, line: int,
) -> Optional[str]:
    """Return the name of the innermost function/class/method whose
    ``[start, end]`` span contains ``line`` in ``file_rel``, or
    ``None`` when nothing covers the line.

    Returns ``None`` for any of:

    * No graph on disk at ``root``.
    * Graph predates ``end_line`` (extractor v1) — the file index
      contains zero rows for the cited file even if v1 once had
      function nodes at it.
    * The file isn't covered by the graph at all.
    * The line is in module scope (between defs / inside imports /
      inside the trailing ``if __name__`` block of a file whose
      classes / funcs don't enclose it).

    The "innermost" pick is by SMALLEST span so a method inside a
    class wins over the enclosing class for the same line. Ties
    (impossible in practice since two siblings can't both contain
    the line) break by the first-seen entry in graph node order.
    """
    if line < 1:
        return None
    index = _load_file_index(root)
    rows = index.get(file_rel)
    if not rows:
        return None
    best_name: Optional[str] = None
    best_span: int = 10**9
    for start, end, name in rows:
        if not (start <= line <= end):
            continue
        span = end - start
        if span < best_span:
            best_span = span
            best_name = name
    return best_name


__all__ = [
    "enclosing_symbol_at",
    "graph_covers_file",
    "clear_cache",
]
