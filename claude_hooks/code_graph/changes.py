"""
Git-diff blast-radius analyser.

Given the project's current diff (vs HEAD by default), find every
symbol defined in the changed files and walk upstream callers via
``code_graph.impact``. Output a markdown report sorted by how much of
the codebase a given change might disturb.

Mirrors gitnexus's ``detect_changes`` MCP tool. Use as a pre-commit
sanity check or in PR descriptions.

Public API:

    git_changed_files(root, *, base="HEAD", include_untracked=False) -> list[str]
    symbols_in_files(graph, files) -> list[node]
    blast_radius(graph, files, *, max_depth=5) -> list[BlastEntry]
    format_blast_radius_report(graph, entries) -> str
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from claude_hooks.code_graph.impact import (
    callers_of,
    files_touched,
    load_graph,
)

log = logging.getLogger("claude_hooks.code_graph.changes")


@dataclass
class BlastEntry:
    """One changed symbol + its impact set."""
    node: dict
    callers: list[tuple[str, int]] = field(default_factory=list)

    @property
    def caller_count(self) -> int:
        return len(self.callers)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def git_changed_files(
    root: Path,
    *,
    base: str = "HEAD",
    include_untracked: bool = False,
) -> list[str]:
    """Files that differ between ``base`` and the working tree.

    Returns paths relative to ``root``. Empty list on any git failure.
    Includes both staged and unstaged changes (``git diff --name-only``
    + ``git diff --cached --name-only`` deduplicated).
    """
    files: set[str] = set()
    for args in (["diff", "--name-only", base],
                 ["diff", "--cached", "--name-only", base]):
        out = _git(root, args)
        if out is None:
            continue
        files.update(line.strip() for line in out.splitlines() if line.strip())

    if include_untracked:
        out = _git(root, ["ls-files", "--others", "--exclude-standard"])
        if out is not None:
            files.update(line.strip() for line in out.splitlines() if line.strip())

    return sorted(files)


def _git(root: Path, args: list[str]) -> Optional[str]:
    try:
        cp = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            log.debug("git %s failed: %s", args, cp.stderr.strip())
            return None
        return cp.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("git %s errored: %s", args, e)
        return None


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------

def symbols_in_files(graph: dict, files: list[str]) -> list[dict]:
    """All function/method/class nodes whose ``file`` is in ``files``."""
    target_set = set(files)
    out = []
    for n in graph.get("nodes") or ():
        if n.get("type") not in ("function", "method", "class"):
            continue
        if n.get("file") in target_set:
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------

def blast_radius(
    graph: dict,
    files: list[str],
    *,
    max_depth: int = 5,
) -> list[BlastEntry]:
    """For every symbol defined in ``files``, walk callers up to depth.

    Returns entries sorted by caller_count desc — the most-disturbing
    symbols come first.
    """
    entries: list[BlastEntry] = []
    for sym in symbols_in_files(graph, files):
        callers = callers_of(graph, sym["id"], max_depth=max_depth)
        entries.append(BlastEntry(node=sym, callers=callers))
    entries.sort(key=lambda e: -e.caller_count)
    return entries


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def format_blast_radius_report(
    graph: dict,
    entries: list[BlastEntry],
    *,
    base: str = "HEAD",
    cap: int = 25,
) -> str:
    """Markdown report. One-line summary up top, then per-symbol detail."""
    out: list[str] = [f"# Blast radius vs `{base}`", ""]

    if not entries:
        out.append("_No tracked symbols in the changed files. The diff "
                   "may be docs/config/test-only, or the graph hasn't "
                   "indexed those files (run `python -m claude_hooks.code_graph build`)._")
        return "\n".join(out)

    total_callers = sum(e.caller_count for e in entries)
    affected_files: set[str] = set()
    for e in entries:
        for cid, _ in e.callers:
            node = next((n for n in graph.get("nodes") or ()
                         if n.get("id") == cid), None)
            if node:
                affected_files.add(node.get("file") or "")

    out.append(f"**{len(entries)} changed symbol(s)** → "
               f"{total_callers} transitive caller(s) across "
               f"**{len(affected_files)} file(s)**.")
    out.append("")
    out.append("Sorted by impact (most-called first):")
    out.append("")

    for e in entries[:cap]:
        n = e.node
        out.append(
            f"## `{n.get('qualname') or n.get('name')}` "
            f"({n.get('type')}, {n.get('file')}:{n.get('line')})"
        )
        if not e.callers:
            out.append("_No transitive callers — safe to refactor (or unused/dead)._")
            out.append("")
            continue
        files = files_touched(graph, [c[0] for c in e.callers])
        out.append(f"{e.caller_count} caller(s) across {len(files)} file(s):")
        for fname, ids in sorted(files.items(), key=lambda kv: -len(kv[1]))[:8]:
            out.append(f"- `{fname}` — {len(ids)} symbol(s)")
        if len(files) > 8:
            out.append(f"- _… + {len(files) - 8} more files_")
        out.append("")

    if len(entries) > cap:
        out.append(f"_… + {len(entries) - cap} more changed symbols_")

    return "\n".join(out)


def run_for_root(
    root: Path,
    *,
    base: str = "HEAD",
    max_depth: int = 5,
    include_untracked: bool = False,
) -> str:
    """One-shot: load graph, diff, compute, render. Always returns markdown.

    On failures (no graph, no git) returns a short explanation instead
    of raising — designed to be wired into hook output directly.
    """
    graph = load_graph(root)
    if not graph:
        return ("_No code graph at `graphify-out/graph.json` — "
                "run `python -m claude_hooks.code_graph build` first._")
    files = git_changed_files(root, base=base, include_untracked=include_untracked)
    if not files:
        return f"_No changes vs `{base}`._"
    entries = blast_radius(graph, files, max_depth=max_depth)
    return format_blast_radius_report(graph, entries, base=base)
