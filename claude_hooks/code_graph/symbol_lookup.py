"""
Per-call symbol lookup for the PreToolUse hook.

Goal: when the model calls ``Grep`` searching for a symbol that exists
in our pre-built graph, inject a one-line answer ("defined at file:line,
N callers") instead of letting it grep the whole tree.

Design constraints (from the brainstorm — keep them visible because
this is the bit most likely to bloat over time):

1. **Reject more than we accept.** The cost of a false-positive
   injection (noise + tokens + latency) is greater than the cost of a
   missed opportunity (silent fallback to normal grep).
2. **Stoplist + heuristic, not regex parser.** Identifier-shape only;
   anything regex-flavoured (``|`` / ``[`` / ``\\b`` / leading ``^``)
   is bypassed because it almost never targets a single symbol.
3. **Budget: ≤50 ms.** Pattern check + dict lookup + format. We never
   hold the hook longer than that. The lookup itself is O(1); the
   index load is one-time per mtime.
4. **One-line lookup answer is the default.** 2-5 hits is acceptable
   as a short list; > 5 hits → bail (the grep is the right tool).
5. **No state, no I/O, no network on the hot path.** Index lives in
   process memory keyed by graph.json mtime.

Public API:

    inject_for_grep(pattern, project_root, *, max_hits=5) -> Optional[str]

Returns the markdown block to inject, or None if there's nothing to
say. Never raises — callers do not need a try/except.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

from claude_hooks.code_graph.detect import graph_json_path

log = logging.getLogger("claude_hooks.code_graph.symbol_lookup")

# Identifier shape — letters, digits, underscores, optional dotted path.
# Length floor is 4 to skip generic words ("get", "set", "id", "fn").
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_MIN_IDENT_LEN = 4

# Patterns that scream "regex, not a symbol". Cheap substring checks —
# any one of these → bail. Order doesn't matter; a single hit is enough.
_REGEX_HINTS = (
    "|", "(", ")", "[", "]", "{", "}",
    "\\b", "\\d", "\\s", "\\w", "\\.",
    ".*", ".+", "?:", "(?",
)

# Common English / boilerplate words that show up as Grep patterns but
# are too noisy as symbol lookups. Conservative — add when we see real
# false-positive injections, not preemptively.
_STOPWORDS = frozenset({
    "error", "errors", "test", "tests", "todo", "fixme",
    "import", "imports", "export", "exports",
    "true", "false", "none", "null", "undefined",
    "value", "values", "type", "types", "data", "info",
    "main", "init", "const", "func", "class", "function",
    "return", "yield", "self", "this", "args", "kwargs",
})

# File extensions — when the leaf of a dotted pattern is one of these,
# the pattern is almost certainly a filename (``config.py``, ``app.tsx``)
# not a symbol. Skip the lookup; the model is doing a path-search.
_FILE_EXT_LEAVES = frozenset({
    "py", "js", "jsx", "ts", "tsx", "mjs", "cjs",
    "go", "rs", "rb", "java", "kt", "scala",
    "c", "h", "cc", "cpp", "hpp", "cs",
    "swift", "lua", "php", "dart",
    "html", "css", "scss", "less",
    "json", "yaml", "yml", "toml", "ini", "cfg",
    "md", "txt", "rst", "log",
    "sh", "bash", "zsh", "fish", "ps1",
    "sql", "graphql", "proto",
})


def looks_like_symbol(pattern: str) -> bool:
    """True iff ``pattern`` looks like a single identifier worth a lookup.

    Conservative — false negatives (skipping a valid symbol) are silent;
    false positives (injecting on noise) are visible. Bias toward skip.
    """
    if not pattern:
        return False
    p = pattern.strip()
    if len(p) < _MIN_IDENT_LEN:
        return False
    # Cheap regex sniff first — it's the most common reject path.
    for hint in _REGEX_HINTS:
        if hint in p:
            return False
    if not _IDENT_RE.match(p):
        return False
    # Only the trailing identifier portion is checked against stopwords
    # — ``error`` is noise but ``MyError`` and ``error_codes`` are real.
    leaf = p.rsplit(".", 1)[-1].lower()
    if leaf in _STOPWORDS:
        return False
    # Reject dotted patterns whose leaf is a file extension —
    # ``config.py`` / ``app.tsx`` are filename searches, not symbols.
    # The check is gated on the ``.`` because we don't want to forbid
    # bare ``py`` etc. (already filtered by the length floor anyway).
    if "." in p and leaf in _FILE_EXT_LEAVES:
        return False
    return True


# ---------------------------------------------------------------------------
# Index loading — mtime-cached, in-process.
# ---------------------------------------------------------------------------

# Per-process cache: {project_root: (graph_mtime, index_dict)}
# Index dict shape: {bare_name: [{"qualname", "id", "type", "file", "line",
#                                 "in_degree", "out_degree"}, ...]}
_INDEX_CACHE: dict[str, tuple[float, dict[str, list[dict]]]] = {}


def _load_index(root: Path) -> dict[str, list[dict]]:
    """Return the symbol index for ``root``, building if missing/stale.

    Empty dict on any failure (no graph, parse error, etc.) — caller
    treats that as "nothing to inject".
    """
    gj = graph_json_path(root)
    if not gj.exists():
        return {}
    try:
        mtime = gj.stat().st_mtime
    except OSError:
        return {}

    cache_key = str(root)
    cached = _INDEX_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        import json
        payload = json.loads(gj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _INDEX_CACHE[cache_key] = (mtime, {})
        return {}

    # Compute degrees once.
    in_deg: defaultdict[str, int] = defaultdict(int)
    out_deg: defaultdict[str, int] = defaultdict(int)
    for e in payload.get("links") or ():
        in_deg[e.get("target")] += 1
        out_deg[e.get("source")] += 1

    index: defaultdict[str, list[dict]] = defaultdict(list)
    for n in payload.get("nodes") or ():
        kind = n.get("type")
        if kind not in ("function", "method", "class"):
            continue
        name = n.get("name") or ""
        if not name:
            continue
        nid = n.get("id")
        index[name].append({
            "qualname": n.get("qualname") or name,
            "id": nid,
            "type": kind,
            "file": n.get("file") or "",
            "line": n.get("line") or 0,
            "in_degree": in_deg.get(nid, 0),
            "out_degree": out_deg.get(nid, 0),
        })

    result = dict(index)
    _INDEX_CACHE[cache_key] = (mtime, result)
    return result


def clear_cache() -> None:
    """Drop the in-process index cache (test helper)."""
    _INDEX_CACHE.clear()


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def inject_for_grep(
    pattern: str,
    project_root: Path,
    *,
    max_hits: int = 5,
) -> Optional[str]:
    """Return the additionalContext block for a Grep call, or None.

    Never raises. Roughly:

      pattern not symbol-shaped       → None
      no graph / index empty          → None
      0 hits                          → None
      1 hit                           → "## Symbol `X`\\n- file:line ..."
      2..max_hits hits                → bulleted list of hits
      >max_hits hits                  → None  (let the grep do its job)
    """
    try:
        if not looks_like_symbol(pattern):
            return None
        index = _load_index(project_root)
        if not index:
            return None

        leaf = pattern.rsplit(".", 1)[-1]
        hits = index.get(leaf, [])
        # If pattern was qualified, narrow further.
        if "." in pattern:
            hits = [h for h in hits if h["qualname"].endswith(pattern)]
        if not hits:
            return None
        if len(hits) > max_hits:
            return None

        return _format(pattern, hits)
    except Exception as e:  # never raise from a hook
        log.debug("inject_for_grep failed: %s", e)
        return None


def _format(pattern: str, hits: list[dict]) -> str:
    if len(hits) == 1:
        h = hits[0]
        return (
            f"## Symbol lookup: `{pattern}`\n\n"
            f"- `{h['qualname']}` ({h['type']}) at `{h['file']}:{h['line']}` "
            f"— {h['in_degree']} callers / {h['out_degree']} callees "
            f"(see `graphify-out/graph.json` for the full call set)\n"
        )
    lines = [f"## Symbol lookup: `{pattern}` ({len(hits)} matches)\n"]
    # Sort by in_degree desc so the most-called ones come first.
    for h in sorted(hits, key=lambda x: -x["in_degree"]):
        lines.append(
            f"- `{h['qualname']}` ({h['type']}) at "
            f"`{h['file']}:{h['line']}` — {h['in_degree']} callers"
        )
    lines.append("\n_Full graph: `graphify-out/graph.json`._")
    return "\n".join(lines)
