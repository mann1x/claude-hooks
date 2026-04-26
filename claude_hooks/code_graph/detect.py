"""
Detection helpers — is this a code repo? Is the graph stale?

Mirrors the pattern in ``claude_hooks.claudemem_reindex``:

- ``project_root(cwd)`` walks up looking for ``.git/``.
- ``is_code_repo(root)`` requires the project to have ≥ ``min_source_files``
  source files in extensions tree-sitter (and our stdlib-ast fallback)
  can handle. Below the threshold we no-op — building a graph for a 2-file
  repo costs more than it saves.
- ``is_graph_stale(root)`` compares ``graphify-out/graph.json`` mtime
  against the latest tracked source mtime, with a cooldown so we don't
  rebuild on every keystroke.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

# Output layout (kept compatible with graphify).
GRAPH_DIRNAME = "graphify-out"
GRAPH_JSON_FILENAME = "graph.json"
GRAPH_REPORT_FILENAME = "GRAPH_REPORT.md"
META_FILENAME = "_meta.json"
CACHE_DIRNAME = "cache"

# Languages we extract today. Python is fully supported via stdlib ``ast``;
# the others are recognised so the detector returns True (a future
# tree-sitter backend can fill in the actual extraction). Until then they
# show up only in the report's "untracked languages" footnote.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".py",
    # Reserved for future tree-sitter backend (detector counts them so
    # mixed-language repos still trigger graph creation).
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".scala",
    ".c", ".h", ".cc", ".cpp", ".hpp",
    ".rb", ".php", ".swift", ".lua", ".cs",
    ".vue", ".svelte", ".dart", ".ex", ".exs",
})

# Languages our current Pass-1 extractor handles end-to-end.
# (When tree_sitter is added, expand this set.)
EXTRACTABLE_EXTENSIONS: frozenset[str] = frozenset({".py"})

# Directories we never count or walk into. Same set claudemem_reindex uses.
IGNORED_DIRS: frozenset[str] = frozenset({
    ".git", ".claudemem", ".caliber", ".wolf",
    "node_modules", "__pycache__", ".venv", "venv", ".env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".cache", ".npm", ".yarn",
    "dist", "build", "target", "out",
    "graphify-out",  # don't recurse into our own output
})


def project_root(cwd: str) -> Optional[Path]:
    """Walk up from ``cwd`` until we find a ``.git`` entry, or None."""
    if not cwd:
        return None
    p = Path(cwd).resolve()
    while True:
        if (p / ".git").exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


def graph_dir(root: Path) -> Path:
    return root / GRAPH_DIRNAME


def graph_json_path(root: Path) -> Path:
    return graph_dir(root) / GRAPH_JSON_FILENAME


def graph_report_path(root: Path) -> Path:
    return graph_dir(root) / GRAPH_REPORT_FILENAME


def is_code_repo(
    root: Path,
    *,
    min_source_files: int = 5,
    max_files_to_scan: int = 2000,
    ignored_dirs: Optional[frozenset[str]] = None,
) -> bool:
    """True iff ``root`` contains at least ``min_source_files`` source files.

    Walks the tree but bails as soon as the threshold is hit, so the
    common case (a real codebase) is O(min_source_files) not O(N).
    """
    ignored = ignored_dirs if ignored_dirs is not None else IGNORED_DIRS
    seen = 0
    walked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignored]
        for fn in filenames:
            walked += 1
            if walked > max_files_to_scan:
                return seen >= min_source_files
            ext = os.path.splitext(fn)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                seen += 1
                if seen >= min_source_files:
                    return True
    return seen >= min_source_files


def is_graph_stale(
    root: Path,
    *,
    cooldown_minutes: int = 10,
    max_files_to_scan: int = 2000,
    ignored_dirs: Optional[frozenset[str]] = None,
) -> bool:
    """True iff the graph should be rebuilt.

    Same semantics as ``reindex_if_stale_async``:

    1. If ``graph.json`` is missing → stale.
    2. Else, if it was rebuilt within the cooldown window → not stale,
       regardless of source churn (prevents thrash).
    3. Else, if any source file's mtime exceeds the graph's mtime →
       stale. Bails on first hit.
    4. Else → not stale.
    """
    gj = graph_json_path(root)
    if not gj.exists():
        return True
    try:
        graph_mtime = gj.stat().st_mtime
    except OSError:
        return True

    # Cooldown: even if files moved, don't rebuild more than every N minutes.
    if graph_mtime + cooldown_minutes * 60 > time.time():
        return False

    ignored = ignored_dirs if ignored_dirs is not None else IGNORED_DIRS
    walked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignored]
        for fn in filenames:
            walked += 1
            if walked > max_files_to_scan:
                return False
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            try:
                if (Path(dirpath) / fn).stat().st_mtime > graph_mtime:
                    return True
            except OSError:
                continue
    return False
