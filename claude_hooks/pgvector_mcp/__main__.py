"""``python -m claude_hooks.pgvector_mcp`` entry — runs the stdio server."""

from __future__ import annotations

import logging
import sys

from claude_hooks.pgvector_mcp.server import serve_stdio


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
