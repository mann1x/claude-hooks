"""Entry point: ``python -m claude_hooks.caliber_proxy``."""

from __future__ import annotations

import sys

from claude_hooks.caliber_proxy.server import run


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
