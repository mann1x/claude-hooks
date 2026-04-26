"""
MCP server exposing the code_graph features as callable tools.

Run as either:
  - stdio:    python -m claude_hooks.code_graph.mcp_server
  - SSE/HTTP: python -m claude_hooks.code_graph.mcp_server --transport sse --port 38090

Wire it into Claude Code via ``~/.claude.json`` ``mcpServers`` entry.

The tools mirror gitnexus's ``query`` / ``impact`` / ``context`` /
``detect_changes`` MCP tools — without LadybugDB, without Cypher, but
with the same shape so the model uses them the same way.

Optional dep: install ``mcp>=1.0`` (the official Anthropic SDK). When
absent, importing this module raises a clear message instead of a
mysterious traceback. Add the extra:

    pip install 'claude-hooks[mcp-server]'
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.code_graph.mcp_server")


# ---------------------------------------------------------------------------
# Lazy SDK import — fail loudly with a helpful message if absent.
# ---------------------------------------------------------------------------

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover — import-time guard
    raise ImportError(
        "code_graph.mcp_server requires the official MCP Python SDK. "
        "Install with `pip install 'claude-hooks[mcp-server]'` "
        "(or `pip install mcp`)."
    ) from e


# ---------------------------------------------------------------------------
# Helpers — shared default-root resolution
# ---------------------------------------------------------------------------

def _resolve_root(root: Optional[str]) -> Path:
    """If caller didn't pass a root, fall back to the env var or cwd.

    The ``CLAUDE_HOOKS_PROJECT_ROOT`` env var lets a wrapper pin a
    specific project to the server (useful when the server is launched
    by Claude Code with a known cwd but the model passes ambiguous refs).
    """
    if root:
        return Path(root).expanduser().resolve()
    env_root = os.environ.get("CLAUDE_HOOKS_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path.cwd().resolve()


# ---------------------------------------------------------------------------
# Server + tool registrations
# ---------------------------------------------------------------------------

def build_server(name: str = "claude-hooks-code-graph") -> FastMCP:
    """Build and return the FastMCP server with all tools registered.

    Factored out so tests can poke at tool registrations without
    actually starting an event loop.
    """
    mcp = FastMCP(name)

    @mcp.tool()
    def code_graph_lookup(pattern: str, root: Optional[str] = None) -> str:
        """Look up a symbol in the project's code graph.

        Returns a one-line answer (file:line + caller count) when the
        pattern is identifier-shaped and matches 1-5 definitions; empty
        string when no useful answer is available.

        Args:
            pattern: Identifier or qualified name (``foo``, ``pkg.foo``).
                     Regex / file-extension patterns get rejected.
            root:    Project root. Defaults to env or cwd.
        """
        from claude_hooks.code_graph.symbol_lookup import inject_for_grep
        out = inject_for_grep(pattern, _resolve_root(root))
        return out or ""

    @mcp.tool()
    def code_graph_impact(
        symbol: str,
        root: Optional[str] = None,
        max_depth: int = 5,
    ) -> str:
        """Show transitive callers + callees of a symbol (blast radius).

        Use this before refactoring to see what would break, or to
        understand a symbol's place in the call graph.

        Args:
            symbol:    Name, qualname, or node id.
            root:      Project root.
            max_depth: BFS cap; 0 = unbounded. Default 5.
        """
        from claude_hooks.code_graph.impact import (
            callees_of, callers_of, format_disambig,
            format_impact_report, load_graph, name_candidates,
            resolve_target,
        )
        graph = load_graph(_resolve_root(root))
        if not graph:
            return ("No code graph at `graphify-out/graph.json` — "
                    "run `python -m claude_hooks.code_graph build` first.")
        depth = None if max_depth == 0 else max_depth
        target = resolve_target(graph, symbol)
        if target is None:
            cands = name_candidates(graph, symbol)
            if cands:
                return format_disambig(cands)
            return f"No symbol matching `{symbol}` in graph."
        callers = callers_of(graph, target, max_depth=depth)
        callees = callees_of(graph, target, max_depth=depth)
        return format_impact_report(graph, target, callers, callees)

    @mcp.tool()
    def code_graph_changes(
        root: Optional[str] = None,
        base: str = "HEAD",
        max_depth: int = 5,
        include_untracked: bool = False,
    ) -> str:
        """Blast-radius report for the current git diff.

        Useful as a pre-commit / pre-push sanity check or for PR
        description generation.

        Args:
            root:              Project root.
            base:              Diff base. ``HEAD`` covers stages + working
                               tree; pass ``origin/main`` for full PR.
            max_depth:         BFS cap on callers per changed symbol.
            include_untracked: Also analyse new files git hasn't tracked.
        """
        from claude_hooks.code_graph.changes import run_for_root
        return run_for_root(
            _resolve_root(root),
            base=base,
            max_depth=max_depth,
            include_untracked=include_untracked,
        )

    @mcp.tool()
    def code_graph_trace(
        entrypoint: str,
        root: Optional[str] = None,
        max_depth: int = 8,
        max_nodes: int = 200,
    ) -> str:
        """Forward call-chain trace from an entrypoint.

        Use to answer "how does X flow through the system?"

        Args:
            entrypoint: Name / qualname / id of the starting function.
            root:       Project root.
            max_depth:  Hops to follow (default 8).
            max_nodes:  Cap on total reachable nodes (default 200).
        """
        from claude_hooks.code_graph.impact import (
            format_disambig, load_graph, name_candidates, resolve_target,
        )
        from claude_hooks.code_graph.trace import format_trace_report, trace
        graph = load_graph(_resolve_root(root))
        if not graph:
            return "No code graph — run `python -m claude_hooks.code_graph build` first."
        target = resolve_target(graph, entrypoint)
        if target is None:
            cands = name_candidates(graph, entrypoint)
            if cands:
                return format_disambig(cands)
            return f"No symbol matching `{entrypoint}` in graph."
        t = trace(graph, target, max_depth=max_depth, max_nodes=max_nodes)
        return format_trace_report(graph, t)

    @mcp.tool()
    def code_graph_mermaid(
        root: Optional[str] = None,
        center: Optional[str] = None,
        depth: int = 2,
        top_n: int = 15,
    ) -> str:
        """Render a Mermaid diagram of the project's code structure.

        With ``center=None``: top-N module map + import edges.
        With ``center=<symbol>``: local subgraph around the symbol.

        Output is plain Mermaid markdown (no ```mermaid fence).

        Args:
            root:   Project root.
            center: Optional symbol to focus on; if set, render a
                    local subgraph instead of the global map.
            depth:  Subgraph depth (only when ``center`` is set).
            top_n:  Module count for the global map.
        """
        from claude_hooks.code_graph.impact import load_graph, resolve_target
        from claude_hooks.code_graph.mermaid import (
            render_module_map, render_subgraph,
        )
        graph = load_graph(_resolve_root(root))
        if not graph:
            return "flowchart LR\n    empty[\"no graph — run build first\"]\n"
        if center:
            target = resolve_target(graph, center)
            if target is None:
                return f"flowchart LR\n    empty[\"unknown symbol: {center}\"]\n"
            return render_subgraph(graph, target, depth=depth)
        return render_module_map(graph, top_n=top_n)

    @mcp.tool()
    def code_graph_companions(root: Optional[str] = None) -> str:
        """Report which optional code-intelligence tools are detected.

        Returns a JSON dump showing whether code_graph is built and
        whether gitnexus is installed/indexed.

        Args:
            root: Project root.
        """
        from claude_hooks.code_graph.detect import (
            graph_json_path, graph_report_path,
        )
        try:
            from claude_hooks.gitnexus_integration import status as gn_status
        except Exception:
            gn_status = lambda _r: {"binary": None, "project_indexed": False}  # type: ignore
        r = _resolve_root(root)
        return json.dumps({
            "code_graph": {
                "graph_json": str(graph_json_path(r)),
                "exists": graph_json_path(r).exists(),
                "report": str(graph_report_path(r)),
            },
            "gitnexus": gn_status(r),
        }, indent=2)

    return mcp


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m claude_hooks.code_graph.mcp_server",
        description="MCP server exposing the code_graph features as tools.",
    )
    ap.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="Transport layer (default: stdio for direct MCP host wiring).",
    )
    ap.add_argument("--host", default="127.0.0.1",
                    help="HTTP bind host (sse/streamable-http only).")
    ap.add_argument("--port", type=int, default=38090,
                    help="HTTP bind port (sse/streamable-http only).")
    ap.add_argument("--name", default="claude-hooks-code-graph",
                    help="MCP server name advertised to clients.")
    args = ap.parse_args(argv)

    server = build_server(args.name)
    if args.transport == "stdio":
        server.run()
    else:
        # FastMCP exposes per-transport runners under run() with kwargs.
        server.settings.host = args.host
        server.settings.port = args.port
        server.run(transport=args.transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
