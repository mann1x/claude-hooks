"""SQLite + sqlite-vec MCP server — exposes the claude-hooks local
sqlite_vec store as a proper stdio JSON-RPC MCP server so external
clients (Cursor, Codex, OpenWebUI, Claude Desktop) can recall + store
directly against the same ``.db`` file the hook pipeline uses.

Tools exposed (memory-only in v1.6; FTS5 hybrid + KG are deferred):

- ``sqlite-vec-find``   — pure vector recall (cosine distance)
- ``sqlite-vec-store``  — insert one memory
- ``sqlite-vec-count``  — count rows in the configured primary table

Transport: stdio JSON-RPC 2.0, MCP protocolVersion 2024-11-05.

Run as:

    python -m claude_hooks.sqlite_vec_mcp

The handshake is the standard ``initialize`` →
``notifications/initialized`` → ``tools/list`` → ``tools/call`` flow.
SQLite serialises writers on a single ``.db`` file, so two MCP
clients storing simultaneously will queue — that's a property of
the underlying engine, not this server.
"""

from claude_hooks.sqlite_vec_mcp.server import McpServer, serve_stdio

__all__ = ["McpServer", "serve_stdio"]
