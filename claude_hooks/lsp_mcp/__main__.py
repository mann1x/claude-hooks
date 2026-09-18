"""``python -m claude_hooks.lsp_mcp`` — stdio MCP server.

Wired into a client the same way cclsp was, but pointing here::

    "lsp": {
      "command": "/path/to/claude-hooks/bin/claude-hook-lsp-mcp",
      "args": []
    }

Logs go to stderr; stdout carries JSON-RPC only, so anything written
there by mistake corrupts the stream.
"""
from __future__ import annotations

import argparse
import logging
import sys

from claude_hooks.lsp_mcp.server import (
    DEFAULT_IDLE_HOURS,
    EngineRegistry,
    serve_stdio,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="claude_hooks.lsp_mcp")
    p.add_argument(
        "--idle-hours", type=float, default=None,
        help=f"Stop language servers unqueried for this long "
             f"(default {DEFAULT_IDLE_HOURS}, or env LSP_MCP_IDLE_HOURS)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    registry = (EngineRegistry(idle_hours=args.idle_hours)
                if args.idle_hours is not None else None)
    return serve_stdio(registry)


if __name__ == "__main__":
    sys.exit(main())
