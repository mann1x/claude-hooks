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
    # M14: thread cfg + base_url so the app factory can spawn the
    # store reaper when ``cfg.store.enabled`` and TTL / distillation
    # are configured. Older callers that don't pass these continue
    # to work — the reaper is opt-in.
    # ``resume_inflight``: councils the previous engine left unfinished
    # (suspended at shutdown, or killed) resume under their own sids.
    app = create_app(
        run_council=runner, run_follow_up=follow_up_runner,
        cfg=cfg, ollama_base_url=base_url,
        resume_inflight=True,
    )

    # Open SSE streams must not hold the shutdown; the councils behind
    # them are suspended by the app's shutdown hook either way.
    uvicorn.run(app, host=args.host, port=port, log_config=None,
                timeout_graceful_shutdown=5)
    if getattr(app.state, "shutdown_unfinished", 0):
        # Runner threads still inside an LLM call past the grace.
        # Joining them (the executor's atexit hook would) holds the
        # process until systemd kills it; their work since the last
        # checkpoint is re-run on resume anyway.
        logging.shutdown()
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
