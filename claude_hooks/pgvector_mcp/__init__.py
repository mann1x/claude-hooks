"""Pgvector MCP server — exposes the claude-hooks pgvector store as a
proper stdio JSON-RPC MCP server so external clients (Claude Code,
other MCP-aware tools) can recall + store directly without going
through the hook pipeline.

Tools exposed:

- ``pgvector-find``           — pure vector recall (cosine distance)
- ``pgvector-find-hybrid``    — RRF blend of vector + BM25 keyword
- ``pgvector-store``          — insert one memory
- ``pgvector-count``          — count rows in the configured primary table
- ``pgvector-kg-search``      — search KG nodes (entity name + observations)
- ``pgvector-kg-create``      — bulk-create KG entities
- ``pgvector-kg-observe``     — add observations to an entity
- ``pgvector-kg-relate``      — create relations between entities

Transport: stdio JSON-RPC 2.0, MCP protocolVersion 2024-11-05.

Run as:

    python -m claude_hooks.pgvector_mcp

The handshake is the standard ``initialize`` →
``notifications/initialized`` → ``tools/list`` → ``tools/call`` flow.
"""

from claude_hooks.pgvector_mcp.server import McpServer, serve_stdio

__all__ = ["McpServer", "serve_stdio"]
