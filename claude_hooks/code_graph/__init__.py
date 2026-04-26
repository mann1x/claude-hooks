"""
code_graph — lightweight code-structure graph for claude-hooks.

Borrows the *idea* from graphify (https://github.com/safishamsi/graphify):
build a queryable, file-based graph of the project's code structure
(modules, classes, functions, imports, calls) so the model can orient
itself in one short report instead of grepping its way through the tree.

Differences from graphify:
- **No LLM at build time.** Pass-1-only (deterministic AST extraction).
  Skips graphify's Whisper/Claude-subagent passes — they're what makes
  graphify slow and API-bound. AST + call-graph alone covers the
  orientation use case.
- **No PreToolUse hook.** Injected once at SessionStart as a single
  ~2k-token report (or whatever ``code_graph.max_inject_chars`` allows),
  not before every Glob/Grep.
- **Stdlib-first.** Python files are parsed with the stdlib ``ast``
  module — no dependencies. Tree-sitter for additional languages is a
  future opt-in extra; the public API does not change.

Output layout (compatible with graphify so both tools can co-exist):

    <project>/graphify-out/
      ├── graph.json          # NetworkX node-link format
      ├── GRAPH_REPORT.md     # Human-readable summary
      ├── cache/              # Per-file SHA cache for incremental builds
      └── _meta.json          # {"managed_by": "claude-hooks", ...}

The ``_meta.json`` sidecar lets graphify (or a user) detect when the
output was last produced by claude-hooks vs graphify and decide whether
to overwrite.
"""

from __future__ import annotations

from claude_hooks.code_graph.detect import (
    GRAPH_DIRNAME,
    GRAPH_JSON_FILENAME,
    GRAPH_REPORT_FILENAME,
    is_code_repo,
    is_graph_stale,
    project_root,
)
from claude_hooks.code_graph.inject import build_session_block

__all__ = [
    "GRAPH_DIRNAME",
    "GRAPH_JSON_FILENAME",
    "GRAPH_REPORT_FILENAME",
    "build_session_block",
    "is_code_repo",
    "is_graph_stale",
    "project_root",
]
