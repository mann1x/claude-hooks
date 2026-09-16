"""MCP server exposing the claude-hooks LSP engine.

Drop-in replacement for the third-party ``cclsp`` binary behind the
``lsp`` MCP tools: same twelve tool names, same parameters, our engine
underneath. See :mod:`claude_hooks.lsp_mcp.server` for what changes.
"""

from claude_hooks.lsp_mcp.server import (
    EngineRegistry,
    LspMcpServer,
    find_project_root,
    serve_stdio,
)

__all__ = [
    "EngineRegistry",
    "LspMcpServer",
    "find_project_root",
    "serve_stdio",
]
