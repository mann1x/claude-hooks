"""``python -m consultants.server`` entry point.

Boots the FastAPI app with the production LangGraph runner and
serves on the port from config (default 38095). Used by the
systemd / launchd / Task Scheduler unit AND by the daemon's
smart-start mode (which spawns this on demand).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from consultants import config as cc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="consultants-server",
        description="Run the /consultants HTTP service.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None,
                        help="Override port (defaults to config "
                             "service.http_port).")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = cc.load_config()
    port = args.port if args.port is not None else cfg.service.http_port

    try:
        from consultants.server.app import create_app
        from consultants.server.runner import (
            make_runner, make_follow_up_runner, default_ollama_url,
        )
        import uvicorn
    except ImportError as e:
        print(f"error: missing dependency ({e}). Run "
              f"`python install.py` to set up the "
              f"claude-hooks-consultants conda env.", file=sys.stderr)
        return 2

    base_url = default_ollama_url()
    runner = make_runner(ollama_base_url=base_url)
    follow_up_runner = make_follow_up_runner(ollama_base_url=base_url)
    app = create_app(run_council=runner, run_follow_up=follow_up_runner)

    uvicorn.run(app, host=args.host, port=port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
