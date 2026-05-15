"""``python -m claude_hooks.sqlite_vec_mcp`` entry. Default mode is
stdio (matches the launcher contract used by Claude Code via
``~/.local/bin/sqlite-vec-mcp``). Pass ``--http`` to serve over HTTP
streamable on ``SQLITE_VEC_MCP_HTTP_HOST:SQLITE_VEC_MCP_HTTP_PORT``
(defaults ``0.0.0.0:32777``) — meant for the systemd unit that
fronts Claude Desktop / remote MCP clients.
"""

from __future__ import annotations

import argparse
import logging
import sys

from claude_hooks.sqlite_vec_mcp.server import (
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
    serve_http,
    serve_stdio,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="claude_hooks.sqlite_vec_mcp")
    p.add_argument(
        "--http", action="store_true",
        help="Serve HTTP streamable instead of stdio",
    )
    p.add_argument(
        "--host", default=None,
        help=f"HTTP bind host (default {DEFAULT_HTTP_HOST}, "
             "or env SQLITE_VEC_MCP_HTTP_HOST)",
    )
    p.add_argument(
        "--port", type=int, default=None,
        help=f"HTTP bind port (default {DEFAULT_HTTP_PORT}, "
             "or env SQLITE_VEC_MCP_HTTP_PORT)",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.http:
        return serve_http(host=args.host, port=args.port)
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
