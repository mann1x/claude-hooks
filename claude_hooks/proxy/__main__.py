"""CLI entry: ``python -m claude_hooks.proxy``."""

from claude_hooks.proxy.server import run
import sys

if __name__ == "__main__":
    sys.exit(run())
